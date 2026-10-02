import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.diagnostic_artifacts import diagnostic_path

ROOT=Path(__file__).resolve().parents[1]


class TmpEntryPointTests(unittest.TestCase):
    def test_cli_and_exporter_success_failure_cycles_are_contained(self):
        with tempfile.TemporaryDirectory() as fixture:
            base=Path(fixture).resolve()
            env={**os.environ,'BOOKTEXT_OWNED_TMP_BASE':str(base)}
            for entry in ['booktext.py','tools/export_document.py']:
                for args in [['--help'],['--not-a-real-option']]*2:
                    result=subprocess.run([sys.executable,str(ROOT/entry),*args],env=env,capture_output=True,text=True)
                    self.assertEqual(result.returncode,0 if args==['--help'] else 2,result.stderr)
                    ns=base/f'booktext-owned-v2-{os.getuid()}'
                    self.assertEqual([p for p in ns.iterdir() if not p.name.startswith('.')],[])

    def test_diagnostics_require_explicit_non_tmp_evidence_destination(self):
        with mock.patch.dict(os.environ,{},clear=True):
            with self.assertRaises(RuntimeError):diagnostic_path('audit.json')
        with mock.patch.dict(os.environ,{'BOOKTEXT_DIAGNOSTIC_DIR':'/private/tmp/booktext-evidence'}):
            with self.assertRaises(RuntimeError):diagnostic_path('audit.json')
        with mock.patch.dict(os.environ,{'BOOKTEXT_DIAGNOSTIC_DIR':str(ROOT/'evidence')}):
            self.assertEqual(diagnostic_path('audit.json'),ROOT/'evidence/audit.json')
