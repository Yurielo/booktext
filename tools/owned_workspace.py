"""Ownership-safe lifecycle for BookText's disposable local workspaces.

No function in this module searches for directories by prefix/age.  Cleanup is
possible only with an exact path plus an authenticated BookText marker.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path


MARKER = ".booktext-owned-workspace.json"
SCHEMA = "1.0"


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # We do not own it or cannot inspect it: fail closed.
        return True
    return True


def _marker(path):
    candidate = Path(path) / MARKER
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _owned_marker(path, workspace_id):
    value = _marker(path)
    return value if value and value.get("schema_version") == SCHEMA and value.get("workspace_id") == workspace_id else None


class OwnedWorkspace:
    """A run-owned temporary directory removed by its creating run only."""

    def __init__(self, *, prefix="booktext-owned-"):
        self.path = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        self.workspace_id = str(uuid.uuid4())
        self._closed = False
        marker = {"schema_version": SCHEMA, "artifact_type": "booktext.owned_workspace", "workspace_id": self.workspace_id, "owner_pid": os.getpid(), "path": str(self.path)}
        (self.path / MARKER).write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")

    def __enter__(self):
        return self.path

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def close(self):
        if self._closed:
            return False
        self._closed = True
        # The private UUID prevents deleting a substituted directory even when
        # its name happens to resemble a BookText workspace.
        if _owned_marker(self.path, self.workspace_id) is None:
            return False
        shutil.rmtree(self.path)
        return True


def reap_explicit(path, *, workspace_id):
    """Reap one known stale workspace, refusing unknown or live-owned paths."""
    target = Path(path).expanduser().resolve()
    marker = _owned_marker(target, workspace_id)
    if marker is None:
        return {"reaped": False, "reason": "unowned_or_marker_mismatch"}
    if _pid_alive(marker.get("owner_pid")):
        return {"reaped": False, "reason": "owner_process_is_live"}
    shutil.rmtree(target)
    return {"reaped": True, "reason": "explicit_stale_owned_workspace"}
