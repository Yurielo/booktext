import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

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

    def test_recovery_crops_only_preserved_layout_visuals_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pages").mkdir()
            Image.new("RGB", (100, 200), (40, 80, 120)).save(root / "pages/page-0001.png")
            book = {"pages": [{"page": 1, "text": "plain text", "page_image": "pages/page-0001.png", "visuals": []}]}
            review = {"pages": [{"page": 1, "agreement": .9, "surya_words": 2, "vision_words": 2}], "review": []}
            (root / "book.json").write_text(json.dumps(book), encoding="utf-8")
            review_path = root / "review.json"; review_path.write_text(json.dumps(review), encoding="utf-8")
            results = root / "results.json"
            results.write_text(json.dumps({"page-0001": [{"page": 1, "image_bbox": [0, 0, 100, 200], "blocks": [
                {"label": "Picture", "bbox": [10, 20, 90, 80]},
                {"label": "Text", "bbox": [0, 80, 100, 190]},
                {"label": "Caption", "bbox": [10, 81, 90, 99]},
            ]}]}), encoding="utf-8")
            raw_book = (root / "book.json").read_bytes(); raw_review = review_path.read_bytes()
            first = recover(root, surya_results=results)
            artifact = json.loads((root / "visual_evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(first["visual_evidence"]["recovered_count"], 1)
            self.assertEqual(len(artifact["visuals"]), 1)
            visual = artifact["visuals"][0]
            self.assertEqual(visual["type"], "picture")
            self.assertEqual(visual["bbox"], [.1, .1, .9, .4])
            self.assertEqual(visual["source_provenance"]["layout_label"], "Picture")
            with Image.open(root / visual["image"]) as crop:
                self.assertEqual(crop.size, (80, 60))
            second = recover(root, surya_results=results)
            self.assertEqual(second["visual_evidence"]["recovered_count"], 1)
            self.assertEqual(json.loads((root / "visual_evidence.json").read_text(encoding="utf-8")), artifact)
            self.assertEqual((root / "book.json").read_bytes(), raw_book)
            self.assertEqual(review_path.read_bytes(), raw_review)


if __name__ == "__main__": unittest.main()
