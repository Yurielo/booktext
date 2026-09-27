import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "tools" / "build_review_rtf.py"
SPEC = importlib.util.spec_from_file_location("build_review_rtf", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReviewRtfTests(unittest.TestCase):
    def test_marks_surya_fragment_without_replacing_it(self):
        rtf, report = MODULE.build_rtf(
            {"pages": [{"page": 1, "text": "The Emporors rule."}]},
            {"review": [{"page": 1, "kind": "replace", "surya": "Emporors", "vision": "Emperors"}]},
        )
        self.assertIn(r"{\highlight1\cb1 Emporors\highlight0\cb0}", rtf)
        self.assertIn("Yellow text is an unresolved difference", rtf)
        self.assertNotIn("Emperors", rtf)
        self.assertEqual(report["pages"][0]["highlighted_spans"], 1)

    def test_records_review_item_that_cannot_be_located(self):
        _, report = MODULE.build_rtf(
            {"pages": [{"page": 1, "text": "Known text"}]},
            {"review": [{"page": 1, "kind": "delete", "surya": "missing"}]},
        )
        self.assertEqual(report["pages"][0]["unlocated_review_items"], ["missing"])
