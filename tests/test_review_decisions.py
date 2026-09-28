import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import review_decisions as decisions


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ReviewDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.book_path = self.root / "book.json"
        self.priority_path = self.root / "review_priority.json"
        self.decisions_path = self.root / "review_decisions.json"
        self.book = {
            "pages": [
                {"page": 1, "text": "alpha 1/2 gamma", "visuals": [{"type": "image", "image": "images/a.png", "description": "A"}], "tables": {"tables": []}},
                {"page": 2, "text": "beta delta", "visuals": [], "tables": {"tables": [{"index": 1, "status": "fallback_review"}]}},
            ]
        }
        self.priority = {
            "schema_version": "1.0", "artifact_type": "booktext.review_priority", "counts": {}, "items": [
                {"item_id": "keep", "page": 1, "priority": "MEDIUM", "item_type": "ocr_qa", "surya": "alpha", "vision": "ALPHA", "reasons": [], "signals": {}},
                {"item_id": "numeric", "page": 1, "priority": "HIGH", "item_type": "ocr_qa", "surya": "1/2", "vision": "1/3", "reasons": [], "signals": {}},
                {"item_id": "manual", "page": 1, "priority": "MEDIUM", "item_type": "ocr_qa", "surya": "gamma", "vision": "garnma", "reasons": [], "signals": {}},
                {"item_id": "ignore", "page": 2, "priority": "MEDIUM", "item_type": "ocr_qa", "surya": "beta", "vision": "BETA", "reasons": [], "signals": {}},
                {"item_id": "defer", "page": 2, "priority": "HIGH", "item_type": "ocr_qa", "surya": "delta", "vision": "DELTA", "reasons": [], "signals": {}},
                {"item_id": "table", "page": 2, "priority": "INFO", "item_type": "table_fallback_review", "table_index": 1, "reasons": ["not_verified_structured_data"], "signals": {}},
            ]
        }
        write_json(self.book_path, self.book)
        write_json(self.priority_path, self.priority)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, item, action, replacement=None, replace=False):
        return decisions.record_decision(
            decisions_path=self.decisions_path, priority_path=self.priority_path,
            book_path=self.book_path, item_id=item, action=action,
            replacement_text=replacement, replace=replace,
        )

    def build(self):
        return decisions.build_final(
            decisions.load_json(self.book_path), decisions.load_json(self.priority_path), decisions.load_json(self.decisions_path),
            book_path=self.book_path, priority_path=self.priority_path,
            decisions_path=self.decisions_path, booktext_version="0.1.0",
        )

    def test_keep_accept_manual_ignore_defer_and_table_fallback(self):
        self.record("keep", "keep_surya")
        self.record("numeric", "accept_vision")
        self.record("manual", "manual", "GAMMA")
        self.record("ignore", "ignore")
        self.record("defer", "defer")
        self.record("table", "defer")
        final_text, final_markdown, report = self.build()
        self.assertIn("alpha 1/3 GAMMA", final_text)
        self.assertIn("beta delta", final_text)
        self.assertIn("![image](images/a.png)", final_markdown)
        self.assertIn("fallback review evidence only", final_markdown)
        self.assertEqual(report["applied_text_changes"], 2)
        self.assertEqual(report["decision_status_counts"]["deferred"], 2)
        self.assertEqual(report["unresolved_priority_counts"]["HIGH"], 1)
        self.assertNotIn("cells", _canonical(report))

    def test_invalid_accept_vision_and_manual_are_rejected(self):
        self.priority["items"][0]["vision"] = ""
        write_json(self.priority_path, self.priority)
        with self.assertRaisesRegex(ValueError, "accept_vision"):
            self.record("keep", "accept_vision")
        with self.assertRaisesRegex(ValueError, "manual"):
            self.record("manual", "manual")

    def test_unknown_item_page_mismatch_stale_and_conflicting_duplicates_fail_safely(self):
        with self.assertRaisesRegex(ValueError, "unknown review item"):
            self.record("missing", "keep_surya")
        self.record("keep", "keep_surya")
        document = decisions.load_json(self.decisions_path)
        duplicate = dict(document["decisions"][0])
        duplicate["action"] = "accept_vision"
        document["decisions"].append(duplicate)
        write_json(self.decisions_path, document)
        _, _, report = self.build()
        self.assertEqual(report["stale_or_conflict_count"], 2)
        self.assertEqual({entry["reason"] for entry in report["decisions"]}, {"duplicate_decision_for_item"})

    def test_page_mismatch_and_stale_anchor_do_not_change_raw_text(self):
        self.record("numeric", "accept_vision")
        document = decisions.load_json(self.decisions_path)
        document["decisions"][0]["page"] = 2
        document["book_source"]["sha256"] = decisions.sha256_file(self.book_path)
        write_json(self.decisions_path, document)
        text, _, report = self.build()
        self.assertIn("1/2", text)
        self.assertEqual(report["stale_or_conflict_count"], 1)
        self.assertEqual(report["decisions"][0]["reason"], "page_does_not_match_review_item")

    def test_changed_raw_source_identity_marks_decision_stale(self):
        self.record("numeric", "accept_vision")
        self.book["pages"][0]["text"] = "alpha 1/2 changed"
        write_json(self.book_path, self.book)
        text, _, report = self.build()
        self.assertIn("1/2 changed", text)
        self.assertEqual(report["decisions"][0]["status"], "stale")
        self.assertEqual(report["decisions"][0]["reason"], "source_output_identity_changed")

    def test_optional_location_geometry_does_not_stale_a_decision(self):
        self.record("numeric", "accept_vision")
        priority = decisions.load_json(self.priority_path)
        priority["items"][0].setdefault("signals", {})["vision_bbox"] = [0.1, 0.2, 0.3, 0.4]
        write_json(self.priority_path, priority)
        text, _, report = self.build()
        self.assertIn("1/3", text)
        self.assertEqual(report["stale_or_conflict_count"], 0)

    def test_rebuild_is_idempotent_and_raw_artifacts_unchanged(self):
        self.record("numeric", "accept_vision")
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (self.book_path, self.priority_path, self.decisions_path)}
        first = self.build()
        second = self.build()
        self.assertEqual(first, second)
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})

    def test_adjacent_and_multiple_page_changes_apply_from_raw(self):
        self.book["pages"][0]["text"] = "alpha 1/2gamma"
        write_json(self.book_path, self.book)
        self.record("numeric", "accept_vision")
        self.record("manual", "manual", "GAMMA")
        self.record("ignore", "accept_vision")
        text, _, report = self.build()
        self.assertIn("1/3GAMMA", text)
        self.assertIn("BETA delta", text)
        self.assertEqual(report["applied_text_changes"], 3)

    def test_table_fallback_cannot_invent_text(self):
        with self.assertRaisesRegex(ValueError, "table_fallback"):
            self.record("table", "manual", "invented cells")
        self.record("table", "ignore")
        text, _, report = self.build()
        self.assertEqual(text.count("beta delta"), 1)
        self.assertEqual(report["decision_status_counts"]["resolved_no_change"], 1)

    def test_cli_build_writes_new_outputs_without_subprocesses(self):
        self.record("numeric", "accept_vision")
        output = self.root / "final"
        with patch.object(subprocess, "run", side_effect=AssertionError("must not invoke OCR/model")), patch.object(subprocess, "Popen", side_effect=AssertionError("must not invoke OCR/model")):
            final_text, final_markdown, report = self.build()
            decisions.write_final(output, final_text, final_markdown, report)
        self.assertTrue((output / "book_final.txt").exists())
        self.assertTrue((output / "book_final.md").exists())
        self.assertTrue((output / "final_build.json").exists())


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
