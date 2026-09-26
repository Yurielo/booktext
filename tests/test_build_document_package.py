import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "build_document_package.py"
SPEC = importlib.util.spec_from_file_location("build_document_package", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DocumentPackageTests(unittest.TestCase):
    def test_creates_stable_provenance_blocks_and_cells(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "scan.jpg"
            source.write_bytes(b"source-bytes")
            book = {"ocr_engine": "surya", "ocr_languages": "eng", "pages": [{"page": 1, "page_image": "pages/page-0001.png", "text": "fallback"}]}
            surya = {"page-0001": [{"blocks": [
                {"label": "Text", "reading_order": 0, "bbox": [0, 0, 10, 10], "html": "<p>Visible text</p>", "confidence": 0.9},
                {"label": "Table", "reading_order": 1, "bbox": [0, 11, 10, 20], "html": "<table><tr><td>A</td><td>1.5</td></tr></table>", "confidence": 0.8},
            ]}]}
            document = MODULE.make_document(
                book=book, source_paths=[source], book_sha256="a" * 64, surya=surya,
                review={"review": [{"page": 1, "surya": "one", "vision": "two"}]},
            )
            MODULE.validate_document(document)

            page = document["pages"][0]
            self.assertTrue(document["document_id"].startswith("document-"))
            self.assertEqual(page["blocks"][0]["chosen_text"], "Visible text")
            self.assertEqual(page["blocks"][1]["cells"][0]["raw_text"], "A")
            self.assertEqual(page["blocks"][1]["cells"][1]["chosen_text"], "1.5")
            self.assertEqual(document["review_items"][0]["status"], "OPEN")

    def test_fallback_explicitly_records_missing_block_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "scan.jpg"
            source.write_bytes(b"source-bytes")
            document = MODULE.make_document(
                book={"pages": [{"page": 1, "text": "Visible text"}]}, source_paths=[source],
                book_sha256="b" * 64, surya=None, review=None,
            )
            fallback = document["pages"][0]["blocks"][0]
            self.assertIsNone(fallback["raw_ocr"])
            self.assertIn("block_coordinates_and_raw_ocr_unavailable", fallback["limitations"])


if __name__ == "__main__":
    unittest.main()
