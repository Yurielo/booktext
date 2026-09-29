import json
import tempfile
import unittest
from pathlib import Path

from tools import document_identity


class DocumentIdentityTests(unittest.TestCase):
    def test_portable_deliverable_keeps_source_title_and_canonical_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Моя книга_book"
            root.mkdir()
            original = root / "book_final.txt"
            original.write_text("authoritative text", encoding="utf-8")
            identity = document_identity.initialize(root, ["/input/Моя: книга.pdf"])
            named = document_identity.register_deliverable(root, "book_final.txt", "final text")
            self.assertEqual(identity["title"], "Моя: книга")
            self.assertIn("Моя- книга", str(named))
            self.assertEqual((root / named).read_text(encoding="utf-8"), "authoritative text")
            saved = json.loads((root / "document_identity.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["named_deliverables"]["book_final.txt"], str(named))
            self.assertTrue(original.is_file(), "stable Core path must remain intact")

