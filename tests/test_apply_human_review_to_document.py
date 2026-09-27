import importlib.util
import unittest
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "tools" / "apply_human_review_to_document.py"
SPEC = importlib.util.spec_from_file_location("apply_human_review_to_document", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ApplyHumanReviewTests(unittest.TestCase):
    def test_creates_linked_reviewed_revision(self):
        document = {
            "schema_version": "0.1", "artifact_type": "booktext.document",
            "document_id": "document-1", "revision_id": "revision-raw",
            "sources": [{"source_id": "source-1"}],
            "pages": [{"page_id": "page-1", "page_number": 1, "source_id": "source-1", "blocks": [{"block_id": "block-1", "chosen_text": "bad", "review_history": []}]}],
        }
        imported = {"human_review_status": "COMPLETED", "human_reviewed_text_sha256": "x", "changes": [{"page": 1, "kind": "replace", "baseline": "bad", "reviewed": "good", "status": "ACCEPTED_BY_HUMAN_REVIEW"}]}
        revised = MODULE.apply_review(document, "===== PAGE 1 =====\n\ngood\n", imported, "y")
        self.assertEqual(revised["parent_revision_id"], "revision-raw")
        self.assertNotEqual(revised["revision_id"], "revision-raw")
        self.assertEqual(revised["pages"][0]["human_review"]["reviewed_text"], "good")
        self.assertEqual(revised["assurance"]["human_verification"], "PAGE_TEXT_REVIEWED")

    def test_rejects_incomplete_page_set(self):
        document = {"schema_version": "0.1", "artifact_type": "booktext.document", "document_id": "d", "revision_id": "r", "sources": [{"source_id": "s"}], "pages": [{"page_id": "p", "page_number": 1, "source_id": "s", "blocks": []}]}
        with self.assertRaises(ValueError):
            MODULE.apply_review(document, "===== PAGE 2 =====\n\nwrong\n", {"human_review_status": "COMPLETED", "changes": []}, "y")
