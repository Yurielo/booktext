"""Ownership-safe lifecycle for subprocess trees started by one BookText run."""

from __future__ import annotations

import os
import signal
import subprocess
import time


class ManagedProcess:
    """A child tree isolated in its own POSIX session/process group."""

    def __init__(self, command, **kwargs):
        self.command = [str(value) for value in command]
        self.process = subprocess.Popen(self.command, start_new_session=True, **kwargs)
        self.pgid = os.getpgid(self.process.pid)
        self.cleaned = False

    def cleanup(self, graceful_timeout=2.0):
        """Terminate only this process group; safe to call more than once."""
        if self.cleaned:
            return False
        self.cleaned = True
        try:
            os.killpg(self.pgid, signal.SIGTERM)
        except PermissionError:
            try:
                os.kill(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return False
        except ProcessLookupError:
            return False
        deadline = time.monotonic() + graceful_timeout
        while time.monotonic() < deadline:
            try:
                os.killpg(self.pgid, 0)
            except (ProcessLookupError, PermissionError):
                return True
            time.sleep(0.02)
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
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
