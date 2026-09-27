import os
import signal
import subprocess
import sys
import time
import unittest

from tools.process_lifecycle import LifecycleRegistry


class ProcessLifecycleTests(unittest.TestCase):
    def _alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_completion_cleans_descendant_and_is_idempotent(self):
        registry = LifecycleRegistry()
        child = registry.spawn([sys.executable, "-c", "import subprocess; subprocess.Popen(['sleep','30']); exit()"])
        child.process.wait(timeout=2)
        self.assertTrue(child.cleanup())
        self.assertFalse(child.cleanup())

    def test_forced_cleanup_and_other_group_survives(self):
        registry = LifecycleRegistry()
        owned = registry.spawn([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(30)"])
        other = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            self.assertTrue(registry.cleanup())
            time.sleep(0.1)
            self.assertIsNotNone(owned.process.poll())
            self.assertIsNone(other.poll())
        finally:
            os.killpg(os.getpgid(other.pid), signal.SIGKILL)

    def test_independent_registries_do_not_cross_cleanup(self):
        first, second = LifecycleRegistry(), LifecycleRegistry()
        a = first.spawn(["sleep", "30"]); b = second.spawn(["sleep", "30"])
        try:
            first.cleanup(); time.sleep(0.05)
            self.assertIsNotNone(a.process.poll())
            self.assertIsNone(b.process.poll())
        finally:
            second.cleanup()

    def test_sigint_and_sigterm_handlers_cleanup_owned_group(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig):
                registry = LifecycleRegistry()
                child = registry.spawn(["sleep", "30"])
                with self.assertRaises(KeyboardInterrupt):
                    registry.handle_signal(sig)
                self.assertIsNotNone(child.process.poll())

    def test_failure_path_cleanup(self):
        registry = LifecycleRegistry()
        child = registry.spawn(["sleep", "30"])
        try:
            raise RuntimeError("simulated BookText failure")
        except RuntimeError:
            registry.cleanup()
        self.assertIsNotNone(child.process.poll())
