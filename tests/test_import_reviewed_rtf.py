import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "tools" / "import_reviewed_rtf.py"
SPEC = importlib.util.spec_from_file_location("import_reviewed_rtf", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ImportReviewedRtfTests(unittest.TestCase):
    def test_creates_new_text_revision_and_preserves_raw_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, reviewed = root / "base.rtf", root / "reviewed.rtf"
            baseline.write_bytes(b"base")
            reviewed.write_bytes(b"reviewed")
            with patch.object(MODULE, "rtf_text", side_effect=["PAGE 1\nBad", "PAGE 1\nGood"]):
                report = MODULE.import_review(baseline, reviewed, root / "reviewed.txt", root / "log.json")
            self.assertFalse(report["raw_ocr_modified"])
            self.assertEqual(report["changes"][0]["status"], "ACCEPTED_BY_HUMAN_REVIEW")
            self.assertIn("Good", (root / "reviewed.txt").read_text())
