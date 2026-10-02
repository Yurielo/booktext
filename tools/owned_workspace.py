"""Disposable POSIX workspaces with exact receipts, leases and fail-closed recovery.

Only this private namespace is inspected. No prefix/age search of system TMP,
legacy cleanup, persistent-output registration, or global process-name killing.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

APP = "booktext"
MARKER = f".{APP}-owned-workspace.json"
SCHEMA = "2.0"
MAX_WORKSPACES = 32  # fail closed instead of growing when safe recovery is unavailable


def _pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _identity(path, *, directory=True):
    s = os.lstat(path)
    if s.st_uid != os.getuid() or (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)) is False:
        raise ValueError("foreign or substituted path")
    if directory and stat.S_IMODE(s.st_mode) != 0o700:
        raise ValueError("workspace is not private")
    if not directory and s.st_nlink != 1:
        raise ValueError("linked receipt")
    return [s.st_dev, s.st_ino]


def _read(path):
    _identity(path, directory=False)
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("invalid receipt")
    return value


def _write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


def _other_users(path):
    """Unknown inspection result is busy. Ignore only the inspecting process."""
    tool = shutil.which("lsof")
    if not tool:
        return True
    try:
        result = subprocess.run([tool, "-nP", "-F", "p", "+D", str(path)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode not in (0, 1) or result.stderr.strip():
        return True
    pids = {int(line[1:]) for line in result.stdout.splitlines() if line.startswith("p") and line[1:].isdigit()}
    return bool(pids - {os.getpid()})


def _namespace(base=None):
    base = Path(base or os.environ.get(f"{APP.upper()}_OWNED_TMP_BASE") or tempfile.gettempdir()).resolve()
    root = base / f"{APP}-owned-v2-{os.getuid()}"
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    identity = _identity(root)
    # Serialize initial receipt creation and recovery/registration.
    fd = os.open(root / ".namespace.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        receipt = root / ".namespace.json"
        if not receipt.exists():
            # Refuse adoption of a preexisting directory containing unknown files.
            if set(p.name for p in root.iterdir()) != {".namespace.lock"}:
                raise ValueError("unknown namespace contents")
            _write(receipt, dict(schema=SCHEMA, app=APP, identity=identity, token=str(uuid.uuid4())))
        value = _read(receipt)
        if value.get("schema") != SCHEMA or value.get("app") != APP or value.get("identity") != identity or not value.get("token"):
            raise ValueError("namespace authority mismatch")
        yield root, value
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


_namespace = contextlib.contextmanager(_namespace)


def _validated(path, authority, workspace_id=None):
    identity = _identity(path)
    value = _read(path / MARKER)
    if (value.get("schema_version") != SCHEMA or value.get("artifact_type") != f"{APP}.owned_workspace"
        or value.get("path") != str(path) or value.get("identity") != identity
        or value.get("namespace_token") != authority["token"]
        or value.get("workspace_id") != path.name
        or (workspace_id is not None and value.get("workspace_id") != workspace_id)):
        raise ValueError("ownership mismatch")
    uuid.UUID(value["workspace_id"])
    return value


def _reap(path, authority, workspace_id=None):
    try:
        value = _validated(path, authority, workspace_id)
        if _pid_alive(value.get("owner_pid")):
            return {"reaped": False, "reason": "owner_process_is_live"}
        fd = os.open(path / ".lease", os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"reaped": False, "reason": "live_lease"}
            if _other_users(path):
                return {"reaped": False, "reason": "active_or_uninspectable"}
            _validated(path, authority, workspace_id)
            shutil.rmtree(path)
            return {"reaped": True, "reason": "explicit_stale_owned_workspace"}
        finally:
            os.close(fd)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return {"reaped": False, "reason": "unowned_or_marker_mismatch"}


def recover_stale(*, base_dir=None):
    results = []
    with _namespace(base_dir) as (root, authority):
        for path in root.iterdir():
            if path.name.startswith("."):
                continue
            results.append({"path": str(path), **_reap(path, authority)})
    return results


class OwnedWorkspace:
    def __init__(self, *, prefix=None, base_dir=None):
        if base_dir is None and is_supervised():
            base_dir = os.environ["TMPDIR"]
        # Prefix is retained for API compatibility; authority never uses a name heuristic.
        recover_stale(base_dir=base_dir)
        with _namespace(base_dir) as (root, authority):
            if sum(not p.name.startswith(".") for p in root.iterdir()) >= MAX_WORKSPACES:
                raise RuntimeError("owned TMP capacity exhausted; retain ambiguous/live objects")
            self.path = root / str(uuid.uuid4())
            self.path.mkdir(mode=0o700)
            self.workspace_id = self.path.name
            self._authority = authority
            self._identity = _identity(self.path)
            self._closed = False
            self._lease = os.open(self.path / ".lease", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self._lease, fcntl.LOCK_EX)
            _write(self.path / MARKER, dict(schema_version=SCHEMA, artifact_type=f"{APP}.owned_workspace", workspace_id=self.workspace_id,
                   owner_pid=os.getpid(), path=str(self.path), identity=self._identity, namespace_token=authority["token"]))

    def __enter__(self):
        return self.path

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        if self._closed:
            return False
        try:
            _validated(self.path, self._authority, self.workspace_id)
            if _other_users(self.path):
                return False
            shutil.rmtree(self.path)
            return True
        except (OSError, ValueError, KeyError):
            return False
        finally:
            os.close(self._lease)
            self._closed = True


def reap_explicit(path, *, workspace_id):
    target = Path(path).expanduser().absolute()  # never resolve a substituted symlink
    try:
        with _namespace(target.parent.parent) as (root, authority):
            if target.parent != root:
                raise ValueError("outside namespace")
            return _reap(target, authority, workspace_id)
    except (OSError, ValueError):
        return {"reaped": False, "reason": "unowned_or_marker_mismatch"}


def is_supervised():
    """A valid inherited workspace is required, not a user-set bypass boolean."""
    path = os.environ.get(f"{APP.upper()}_OWNED_TMP_PATH")
    if not path:
        return False
    try:
        target = Path(path)
        with _namespace(target.parent.parent) as (root, authority):
            if target.parent != root or os.environ.get(f"{APP.upper()}_OWNED_TMP_BASE") != str(target.parent.parent):
                return False
            marker = _validated(target, authority)
            return _pid_alive(marker["owner_pid"]) and os.environ.get("TMPDIR") == path
    except (OSError, ValueError, KeyError):
        return False


def supervise(command, *, base_dir=None):
    """Bound child/tool scratch and stop only the exact process group we start."""
    workspace = OwnedWorkspace(base_dir=base_dir)
    with workspace as path:
        env = os.environ.copy()
        env.update(TMPDIR=str(path), TMP=str(path), TEMP=str(path))
        env[f"{APP.upper()}_OWNED_TMP_PATH"] = str(path)
        env[f"{APP.upper()}_OWNED_TMP_LEASE_FD"] = str(workspace._lease)
        env[f"{APP.upper()}_OWNED_TMP_BASE"] = str(path.parent.parent)
        child = subprocess.Popen(command, env=env, start_new_session=True, pass_fds=(workspace._lease,))
        interrupted = []
        previous = {}
        def forward(signum, _frame):
            interrupted.append(signum)
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, forward)
        try:
            while child.poll() is None:
                if interrupted:
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                    break
                time.sleep(0.02)
            code = child.wait()
        finally:
            # Completion can leave children behind. Group membership proves ownership.
            try:
                os.killpg(child.pid, signal.SIGTERM)
                time.sleep(0.05)
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 128 + interrupted[0] if interrupted else (code if code >= 0 else 128 - code)


if __name__ == "__main__":
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("usage: owned_tmp.py -- command [args...]")
    raise SystemExit(supervise(command))
