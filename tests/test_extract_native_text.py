import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[1] / "tools" / "extract_native_text.py"
SPEC = importlib.util.spec_from_file_location("extract_native_text", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NativeTextExtractionTests(unittest.TestCase):
    def test_utf8_text_skips_ocr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "note.md"
            source.write_text("Привет", encoding="utf-8")
            result = MODULE.extract(source)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["pages"][0]["text"], "Привет")

    def test_empty_native_page_requests_ocr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "empty.txt"
            source.write_text("", encoding="utf-8")
            result = MODULE.extract(source)
        self.assertEqual(result["status"], "PARTIAL_OCR_REQUIRED")
        self.assertEqual(result["pages"][0]["status"], "OCR_REQUIRED")

    def test_docx_uses_textutil(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "note.docx"
            source.write_bytes(b"placeholder")
            with patch.object(MODULE, "textutil_text", return_value="Existing text"):
                result = MODULE.extract(source)
        self.assertEqual(result["pages"][0]["text"], "Existing text")

    def test_unhandled_format_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "scan.djvu"
            source.write_bytes(b"placeholder")
            result = MODULE.extract(source)
        self.assertEqual(result["status"], "OCR_REQUIRED")
