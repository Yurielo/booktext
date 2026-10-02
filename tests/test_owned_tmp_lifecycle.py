"""Real fixture subprocesses: no media/model/build artifacts or legacy TMP cleanup."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools.owned_workspace import APP, MARKER, OwnedWorkspace, recover_stale, supervise
import tools.owned_workspace as lifecycle


class OwnedTmpLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.base = Path(self.fixture.name).resolve()
        self.env = {**os.environ, f"{APP.upper()}_OWNED_TMP_BASE": str(self.base), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        self.helper = str(Path(lifecycle.__file__).resolve())

    def objects(self):
        namespace = self.base / f"{APP}-owned-v2-{os.getuid()}"
        return [] if not namespace.exists() else [p for p in namespace.iterdir() if not p.name.startswith('.')]

    def test_success_failure_and_repeated_cycles_have_zero_growth(self):
        before = len(self.objects())
        for code in [0, 7] * 4:
            result = subprocess.run([sys.executable, self.helper, '--', sys.executable, '-c',
                'import os,tempfile; from pathlib import Path; p=Path(tempfile.mkdtemp()); (p/"scratch").write_bytes(b"x"*8192); raise SystemExit('+str(code)+')'], env=self.env)
            self.assertEqual(result.returncode, code)
            self.assertEqual(self.objects(), [])
        print(f'BOUNDED_GROWTH {APP}: 8 cycles, owned before={before}/0 bytes after=0/0 bytes')

    def test_context_error_and_cancellation_cleanup(self):
        for error in [RuntimeError, KeyboardInterrupt]:
            with self.assertRaises(error):
                with OwnedWorkspace(base_dir=self.base) as path:
                    (path/'scratch').write_text('fixture')
                    raise error()
            self.assertEqual(self.objects(), [])

    def start_sleeping(self):
        child = subprocess.Popen([sys.executable, self.helper, '--', sys.executable, '-c',
            'import os,time; from pathlib import Path; p=Path(os.environ["TMPDIR"]); (p/"scratch").write_text("fixture"); print(str(p),os.getpid(),flush=True); time.sleep(60)'], env=self.env, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        line=child.stdout.readline().strip()
        self.addCleanup(child.stdout.close)
        self.assertTrue(line)
        path,pid=line.rsplit(' ',1)
        return child,Path(path),int(pid)

    def test_sigterm_and_sigint_cleanup(self):
        for sig in (signal.SIGTERM,signal.SIGINT):
            child,path,_=self.start_sleeping()
            child.send_signal(sig)
            self.assertEqual(child.wait(timeout=8),128+sig)
            self.assertFalse(path.exists())
            self.assertEqual(self.objects(),[])

    def test_sigkill_recovery_preserves_surviving_child_lease_then_reaps(self):
        supervisor,path,pid=self.start_sleeping()
        supervisor.kill();supervisor.wait(timeout=3)
        try:
            results=recover_stale(base_dir=self.base)
            self.assertTrue(path.exists())
            self.assertFalse(next(r for r in results if r['path']==str(path))['reaped'])
        finally:
            os.killpg(pid,signal.SIGTERM)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and lifecycle._other_users(path):time.sleep(.05)
        with OwnedWorkspace(base_dir=self.base):
            self.assertFalse(path.exists())
        self.assertEqual(self.objects(),[])

    def test_unknown_and_symlink_are_preserved(self):
        with OwnedWorkspace(base_dir=self.base) as path:
            unknown=path.parent/'foreign-booktext-looking-name';unknown.mkdir()
            (unknown/'user-document').write_text('keep')
            link=path.parent/'foreign-link';link.symlink_to(unknown,target_is_directory=True)
            recover_stale(base_dir=self.base)
            self.assertTrue((unknown/'user-document').exists());self.assertTrue(link.is_symlink())
        self.assertTrue(unknown.exists())

    def test_other_live_owner_and_foreign_open_file_are_preserved(self):
        workspace=OwnedWorkspace(base_dir=self.base)
        result=recover_stale(base_dir=self.base)
        self.assertFalse(next(r for r in result if r['path']==str(workspace.path))['reaped'])
        (workspace.path/'scratch').write_text('keep')
        holder=subprocess.Popen([sys.executable,'-c','import sys,time; f=open(sys.argv[1]);print("ready",flush=True);time.sleep(60)',str(workspace.path/'scratch')],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(),'ready')
            self.assertFalse(workspace.close())
            self.assertTrue(workspace.path.exists())
        finally:
            holder.terminate();holder.wait(timeout=3);holder.stdout.close()
        # An interpreter still owning the path is retained until it exits.
        self.assertTrue(workspace.path.exists())

    def test_tampered_marker_and_replaced_inode_are_preserved(self):
        workspace=OwnedWorkspace(base_dir=self.base)
        marker=workspace.path/MARKER
        original=json.loads(marker.read_text());marker.write_text('{}')
        self.assertFalse(workspace.close());self.assertTrue(workspace.path.exists())
        marker.write_text(json.dumps(original))
        # Registry names alone cannot authorize a replacement directory.
        displaced=workspace.path.with_name('displaced');workspace.path.rename(displaced)
        workspace.path.mkdir(mode=0o700)
        (workspace.path/MARKER).write_text(json.dumps(original))
        results=recover_stale(base_dir=self.base)
        self.assertFalse(any(r['reaped'] for r in results))
        self.assertTrue(workspace.path.exists());self.assertTrue(displaced.exists())

    def test_unavailable_open_file_inspection_fails_closed(self):
        workspace=OwnedWorkspace(base_dir=self.base)
        with mock.patch.object(lifecycle.shutil,'which',return_value=None):
            self.assertFalse(workspace.close())
        self.assertTrue(workspace.path.exists())

    def test_unknown_capacity_blocks_creation_instead_of_linear_growth(self):
        with OwnedWorkspace(base_dir=self.base) as path:
            root=path.parent
        for i in range(lifecycle.MAX_WORKSPACES): (root/f'unknown-{i}').mkdir()
        with self.assertRaisesRegex(RuntimeError,'capacity'):
            OwnedWorkspace(base_dir=self.base)
        self.assertEqual(len(self.objects()),lifecycle.MAX_WORKSPACES)


if __name__=='__main__':unittest.main()
