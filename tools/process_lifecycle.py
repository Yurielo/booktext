"""Ownership-safe lifecycle for subprocess trees started by one BookText run."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


class ManagedProcess:
    """One BookText-owned process tree, including descendants that daemonise.

    POSIX process groups cover ordinary descendants, but some tools (notably
    Surya's llama-server helper) create a new session.  We therefore record
    concrete descendants while the direct BookText child is alive.  Cleanup is
    always by those exact recorded PIDs; it never discovers processes by name.
    """

    def __init__(self, command, **kwargs):
        self.command = [str(value) for value in command]
        self.process = subprocess.Popen(self.command, start_new_session=True, **kwargs)
        self.pgid = os.getpgid(self.process.pid)
        self.cleaned = False
        self._owned_pids = {self.process.pid}
        self._ownership_lock = threading.Lock()
        self._monitor_stop = threading.Event()
        self._monitor = threading.Thread(target=self._monitor_descendants, daemon=True)
        self._monitor.start()

    @staticmethod
    def _process_parent_map():
        """Return a best-effort PID→PPID snapshot without name matching."""
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pid=,ppid="], check=False,
                capture_output=True, text=True,
            )
        except OSError:
            return {}
        parents = {}
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                parents[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
        return parents

    def _capture_descendants(self):
        parents = self._process_parent_map()
        if not parents:
            return
        with self._ownership_lock:
            frontier = set(self._owned_pids)
            # Repeatedly expand from already-owned PIDs.  A captured daemon
            # remains owned even after its parent is reparented to launchd.
            while frontier:
                descendants = {
                    pid for pid, parent in parents.items() if parent in frontier
                } - self._owned_pids
                if not descendants:
                    break
                self._owned_pids.update(descendants)
                frontier = descendants

    def _monitor_descendants(self):
        while not self._monitor_stop.is_set():
            self._capture_descendants()
            self._monitor_stop.wait(0.02)

    def _owned_snapshot(self):
        self._capture_descendants()
        with self._ownership_lock:
            return sorted(self._owned_pids)

    @staticmethod
    def _signal_pids(pids, sig):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def cleanup(self, graceful_timeout=2.0):
        """Terminate only this owned tree; safe to call more than once."""
        if self.cleaned:
            return False
        self.cleaned = True
        self._capture_descendants()
        self._monitor_stop.set()
        self._monitor.join(timeout=graceful_timeout)
        owned = self._owned_snapshot()
        try:
            os.killpg(self.pgid, signal.SIGTERM)
        except PermissionError:
            try:
                os.kill(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            # A short-lived child can leave its newly-created process group
            # between spawn and cleanup.  Its direct PID is still owned by
            # this registry, so terminate and reap only that PID rather than
            # treating the missing group as proof that cleanup is complete.
            pass
        # This includes every process group that descendant tools may have
        # created themselves.  PIDs were captured only by PPID lineage while
        # the direct child was still owned by BookText.
        self._signal_pids(owned, signal.SIGTERM)
        deadline = time.monotonic() + graceful_timeout
        while time.monotonic() < deadline:
            alive = []
            for pid in owned:
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except (ProcessLookupError, PermissionError):
                    pass
            if not alive:
                return True
            time.sleep(0.02)
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self._signal_pids(owned, signal.SIGKILL)
        try:
            self.process.wait(timeout=graceful_timeout)
        except subprocess.TimeoutExpired:
            pass
        return True

    def wait(self, **kwargs):
        try:
            return self.process.wait(**kwargs)
        finally:
            # Parent completion does not prove descendants stopped.
            self.cleanup()


class LifecycleRegistry:
    def __init__(self):
        self.children = []

    def spawn(self, command, **kwargs):
        child = ManagedProcess(command, **kwargs)
        self.children.append(child)
        return child

    def cleanup(self):
        changed = False
        for child in reversed(self.children):
            changed = child.cleanup() or changed
        return changed

    def handle_signal(self, _signum=None, _frame=None):
        self.cleanup()
        raise KeyboardInterrupt
