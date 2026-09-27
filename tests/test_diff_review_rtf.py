import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "tools" / "diff_review_rtf.py"
SPEC = importlib.util.spec_from_file_location("diff_review_rtf", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReviewRtfDiffTests(unittest.TestCase):
    def test_emits_candidate_without_applying_it(self):
        report = MODULE.candidates("PAGE 1\nThe Emporors rule.", "PAGE 1\nThe Emperors rule.")
        self.assertEqual(report["changes"][0]["status"], "PENDING_HUMAN_CONFIRMATION")
        self.assertEqual(report["changes"][0]["baseline"], "Emporors")
        self.assertEqual(report["changes"][0]["reviewed"], "Emperors")
