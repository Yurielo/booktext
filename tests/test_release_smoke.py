import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseSmokeTests(unittest.TestCase):
    def test_cli_version(self):
        result = subprocess.run([sys.executable, "booktext.py", "--version"], cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), "booktext 0.1.0")

    def test_native_text_cli_smoke(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "note.md"; output = Path(temp) / "native.json"
            source.write_text("Smoke text", encoding="utf-8")
            subprocess.run([sys.executable, "tools/extract_native_text.py", str(source), "--output", str(output)], cwd=ROOT, check=True)
            self.assertEqual(json.loads(output.read_text())["status"], "COMPLETE")
