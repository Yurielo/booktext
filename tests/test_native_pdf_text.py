import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import booktext


class NativePdfTextTests(unittest.TestCase):
    def test_embedded_text_creates_provenanced_non_ocr_output(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "digital.pdf"; source.write_bytes(b"%PDF")
            args = argparse.Namespace(pages=None, exclude_pages=None)
            def fake_run(command, **_kwargs):
                return type("Result", (), {"stdout": "page one" if command[2] == "1" else "page two"})()
            with patch.object(booktext.shutil, "which", return_value="/usr/bin/pdftotext"), patch.object(booktext, "get_pdf_page_count", return_value=2), patch.object(booktext, "run", fake_run):
                output = booktext.run_native_text_pdf(source, args, 0)
            book = json.loads((output / "book.json").read_text())
            self.assertTrue(book["native_text_only"])
            self.assertFalse(book["ocr_performed"])
            self.assertEqual([page["text"] for page in book["pages"]], ["page one", "page two"])
            self.assertEqual(book["pages"][0]["source_provenance"]["kind"], "pdf_embedded_text")

    def test_empty_embedded_text_refuses_silent_ocr_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "scan.pdf"; source.write_bytes(b"%PDF")
            args = argparse.Namespace(pages=None, exclude_pages=None)
            with patch.object(booktext.shutil, "which", return_value="/usr/bin/pdftotext"), patch.object(booktext, "get_pdf_page_count", return_value=1), patch.object(booktext, "run", return_value=type("Result", (), {"stdout": ""})()):
                with self.assertRaisesRegex(RuntimeError, "refuses to silently fall back"):
                    booktext.run_native_text_pdf(source, args, 0)


if __name__ == "__main__": unittest.main()
