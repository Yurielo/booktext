import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import translation


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.document = {
            "schema_version": "1.0", "artifact_type": "booktext.canonical_document", "document_id": "book-1",
            "metadata": {"title": "Example", "source_page_count": 1},
            "source": {}, "source_pages": [{"source_page": 1, "page_image": None}],
            "review": {"unresolved_priority_counts": {"HIGH": 0, "MEDIUM": 0}}, "notes": [], "warnings": [],
            "blocks": [
                {"block_id": "b1", "type": "heading", "text": "HELLO", "reading_order": 1, "provenance": {"source_page": 1}},
                {"block_id": "b2", "type": "paragraph", "text": "Value 12.5 mg on 1/2 page.", "reading_order": 2, "provenance": {"source_page": 1}},
                {"block_id": "b3", "type": "paragraph", "text": "Русский текст", "reading_order": 3, "provenance": {"source_page": 1}},
                {"block_id": "b4", "type": "page_break", "text": "", "reading_order": 4, "provenance": {"source_page": 1}},
            ],
        }
        (self.root / "book_document.json").write_text(json.dumps(self.document), encoding="utf-8")
        (self.root / "review_decisions.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def fake_metadata(*_args, **_kwargs):
        return {"kind": "ollama", "endpoint": "http://local", "model": "test-model", "model_digest": "sha256:test", "model_size_bytes": 1, "api": "/api/chat"}

    @staticmethod
    def fake_translate(**kwargs):
        source = kwargs["source"]
        translated = {"HELLO": "ПРИВЕТ", "Value 12.5 mg on 1/2 page.": "Значение 12.5 mg на 1/2 странице."}.get(source, source)
        return translated, {"temperature": 0, "response_format": "json", "glossary_json": "{}"}

    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_translation_is_source_anchored_deterministic_and_does_not_edit_cdm(self, _metadata, _translate):
        before = (self.root / "book_document.json").read_bytes()
        result = translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model="test-model")
        self.assertFalse(result["reused"])
        self.assertEqual(before, (self.root / "book_document.json").read_bytes())
        artifact = json.loads((self.root / "translations/en-ru.json").read_text())
        self.assertEqual("booktext.translation", artifact["artifact_type"])
        self.assertEqual(3, len(artifact["blocks"]))
        self.assertEqual("machine_translated", artifact["blocks"][1]["status"])
        self.assertEqual("skipped_already_target_language", artifact["blocks"][2]["status"])
        self.assertTrue((self.root / "translations/book-en-ru.md").is_file())
        self.assertTrue((self.root / "translations/book-en-ru-bilingual.md").is_file())
        reused = translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model="test-model")
        self.assertTrue(reused["reused"])
        self.assertEqual(2, _translate.call_count)

    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_changed_source_marks_existing_translation_stale_and_archives_prior_revision(self, _metadata, _translate):
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model="test-model")
        self.document["blocks"][1]["text"] = "Value 14 mg."
        (self.root / "book_document.json").write_text(json.dumps(self.document), encoding="utf-8")
        stale = translation.validate_translation(self.root, source_language="en", target_language="ru")
        self.assertTrue(stale["stale"])
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model="test-model")
        self.assertTrue(list((self.root / "translations/history").glob("en-ru-*.json")))

    @mock.patch("tools.translation._translate_ollama", return_value=("Значение 12.6 mg на 1/2 странице.", {}))
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_numeric_changes_are_never_silently_verified(self, _metadata, _translate):
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model="test-model")
        artifact = json.loads((self.root / "translations/en-ru.json").read_text())
        number_entry = next(item for item in artifact["blocks"] if item["block_id"] == "b2")
        self.assertEqual("needs_numeric_review", number_entry["status"])
        self.assertEqual("FAIL", number_entry["validation"]["numeric_tokens"])

    def test_direction_and_glossary_validation_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "direction"):
            translation.translate_document(self.root, source_language="en", target_language="en", provider="ollama", model="test-model")
        glossary = self.root / "bad.json"; glossary.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "glossary"):
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model="test-model", glossary_path=glossary)


if __name__ == "__main__":
    unittest.main()
