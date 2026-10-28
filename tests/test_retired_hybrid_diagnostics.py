import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RetiredHybridDiagnosticsTests(unittest.TestCase):
    def test_archival_diagnostics_do_not_start_model_inference(self):
        for name in (
            "hybrid_context_v13.py",
            "hybrid_semantic_audit_v15.py",
            "hybrid_targeted_audit_v16.py",
        ):
            with self.subTest(name=name):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "tools" / name)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("RETIRED_DIAGNOSTIC", result.stdout)
                self.assertEqual("", result.stderr)
