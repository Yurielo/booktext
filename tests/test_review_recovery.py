import json
import tempfile
import unittest
from pathlib import Path

from tools.review_recovery import recover


class ReviewRecoveryTests(unittest.TestCase):
    def test_recovery_is_idempotent_and_records_unavailable_external_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "book.json").write_text(json.dumps({"pages": [{"page": 1, "text": "one", "tables": None}]}), encoding="utf-8")
            (root / "review.json").write_text(json.dumps({"pages": [{"page": 1, "agreement": .9, "surya_words": 1, "vision_words": 1}], "review": [{"page": 1, "kind": "replace", "category": "text", "surya": "one", "vision": "One"}]}), encoding="utf-8")
            first, second = recover(root), recover(root)
            self.assertEqual(first, second)
            self.assertEqual(first["raw_review_difference_count"], 1)
            self.assertEqual(first["external_evidence_unavailable"][0]["page"], 319)
            self.assertEqual(json.loads((root / "review_priority.json").read_text())["counts"]["INFO"], 0)

    def test_recovery_adds_only_unambiguous_preserved_surya_block_location(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = {"page-0001": [{
                "image_bbox": [0, 0, 100, 200],
                "blocks": [{"html": "<p>Before disputed phrase after.</p>", "bbox": [10, 20, 90, 60]}],
            }]}
            results = root / "results.json"
            results.write_text(json.dumps(raw), encoding="utf-8")
            review = {"pages": [{"page": 1, "agreement": .9, "surya_words": 3, "vision_words": 2}], "review": [{
                "page": 1, "kind": "delete", "category": "completeness",
                "surya": "disputed phrase", "vision": "", "vision_bbox": None,
            }]}
            (root / "book.json").write_text(json.dumps({"pages": [{"page": 1, "text": "Before disputed phrase after."}]}), encoding="utf-8")
            review_path = root / "review.json"; review_path.write_text(json.dumps(review), encoding="utf-8")
            summary = recover(root, surya_results=results)
            item = json.loads((root / "review_priority.json").read_text())["items"][0]
            self.assertEqual(item["signals"]["surya_bbox"], [0.1, 0.1, 0.9, 0.3])
            self.assertEqual(item["signals"]["surya_bbox_provenance"], "preserved_surya_text_block")
            self.assertEqual(summary["source_location_evidence"]["located_item_count"], 1)
            self.assertEqual(json.loads(review_path.read_text()), review)  # raw evidence remains immutable


if __name__ == "__main__": unittest.main()
