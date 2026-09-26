import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "build_table_review_package.py"
SPEC = importlib.util.spec_from_file_location("build_table_review_package", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TableReviewPackageTests(unittest.TestCase):
    def _write_results(self, root: Path) -> Path:
        path = root / "surya" / "surya-input" / "results.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"page-0007": [{"blocks": [{"label": "Table", "bbox": [1, 2, 3, 4], "confidence": 0.91, "html": "<table><tr><td>A</td><td>B</td></tr></table>"}]}]}), encoding="utf-8")
        return path

    def test_preserves_raw_table_and_requires_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            results_path = self._write_results(root)
            vision_path = root / "vision" / "page-0007.json"
            vision_path.parent.mkdir()
            vision_path.write_text(json.dumps({"tables": [{}], "lines": [{}, {}]}), encoding="utf-8")
            package = MODULE.build_review_package(page=7, results_path=results_path, vision_path=vision_path)
            self.assertEqual(package["status"], "REVIEW_REQUIRED")
            self.assertTrue(package["review_required"])
            self.assertEqual(package["tables"][0]["grid"], [["A", "B"]])
            self.assertIn("<table>", package["tables"][0]["raw_html"])
            self.assertEqual(package["vision_evidence"]["line_count"], 2)

    def test_marks_missing_vision_and_missing_page_for_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            results_path = self._write_results(Path(temp_dir))
            package = MODULE.build_review_package(page=8, results_path=results_path, vision_path=None)
            self.assertEqual(package["status"], "MISSING_SURYA_PAGE")
            self.assertIn("vision_evidence_unavailable", package["review_reasons"])
            self.assertIn("surya_page_missing", package["review_reasons"])
