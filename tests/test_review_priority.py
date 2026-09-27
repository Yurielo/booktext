import json
import tempfile
import unittest
from pathlib import Path

import booktext
from tools.review_priority import PRIORITIES, prioritise_review, priority_text


def qa_item(page, kind, surya, vision, category="text"):
    return {
        "page": page,
        "kind": kind,
        "category": category,
        "surya": surya,
        "vision": vision,
        "vision_confidence": 0.9,
        "vision_bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
    }


class ReviewPriorityTests(unittest.TestCase):
    def test_numeric_and_fraction_disagreement_are_high(self):
        artifact = prioritise_review(
            [{"page": 5, "agreement": 0.9, "surya_words": 10, "vision_words": 10}],
            [qa_item(5, "replace", "Dose 1/2: 352.05", "Dose 1/3: 352.5")],
            [],
        )
        item = artifact["items"][0]
        self.assertEqual(item["priority"], "HIGH")
        self.assertIn("numeric_disagreement", item["reasons"])
        self.assertIn("fraction_or_ratio_disagreement", item["reasons"])

    def test_completeness_insert_delete_are_high(self):
        artifact = prioritise_review(
            [{"page": 8, "agreement": 0.4, "surya_words": 3, "vision_words": 40}],
            [qa_item(8, "insert", "", "many independent OCR words", "completeness")],
            [],
        )
        self.assertEqual(artifact["items"][0]["priority"], "HIGH")
        self.assertIn("possible_completeness_or_structure_loss", artifact["items"][0]["reasons"])

    def test_harmless_segmentation_is_low_and_deduplicated(self):
        candidate = qa_item(2, "replace", "well-known", "well known")
        artifact = prioritise_review(
            [{"page": 2, "agreement": 0.9, "surya_words": 4, "vision_words": 4}],
            [candidate, dict(candidate)],
            [],
        )
        self.assertEqual(len(artifact["items"]), 1)
        self.assertEqual(artifact["items"][0]["priority"], "LOW")
        self.assertEqual(artifact["items"][0]["duplicate_count"], 2)

    def test_ordinary_lexical_difference_is_medium(self):
        artifact = prioritise_review(
            [{"page": 3, "agreement": 0.9, "surya_words": 5, "vision_words": 5}],
            [qa_item(3, "replace", "dragon", "dragan")],
            [],
        )
        self.assertEqual(artifact["items"][0]["priority"], "MEDIUM")

    def test_table_fallback_is_info_and_never_verified_cells(self):
        artifact = prioritise_review([], [], [{
            "page": 319,
            "tables": {"tables": [{
                "index": 1,
                "status": "fallback_review",
                "source": "surya_table_block",
                "diagnostic": "image_only_or_non_html_table_block",
            }]},
        }])
        item = artifact["items"][0]
        self.assertEqual(item["priority"], "INFO")
        self.assertEqual(item["item_type"], "table_fallback_review")
        self.assertIn("not_verified_structured_data", item["reasons"])
        self.assertNotIn("cells", item)
        self.assertIn("not verified", priority_text(artifact))

    def test_empty_artifact_has_all_priority_counts(self):
        artifact = prioritise_review([], [], [])
        self.assertEqual(artifact["schema_version"], "1.0")
        self.assertEqual(artifact["counts"], {priority: 0 for priority in PRIORITIES})


class RunSummaryTests(unittest.TestCase):
    def test_summary_schema_and_cli_no_high_message(self):
        priority = prioritise_review([], [], [])
        summary = booktext.build_run_summary(
            [{"page": 1, "tables": {"tables": [{"status": "HTML_TABLE"}]}}],
            1,
            1.23456,
            [],
            [],
            priority,
        )
        self.assertEqual(summary["schema_version"], "1.0")
        self.assertEqual(summary["tables"]["structured"], 1)
        self.assertEqual(summary["outputs"]["run_summary"], "run_summary.json")
        self.assertIn("HIGH review: none.", booktext.format_completion_summary(summary, Path("review_priority.txt")))

    def test_priority_outputs_do_not_modify_raw_review_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "review.json"
            raw.write_text('{"raw": true}\n', encoding="utf-8")
            priority_json = root / "review_priority.json"
            priority_txt = root / "review_priority.txt"
            booktext.write_review_priority_outputs(priority_json, priority_txt, [], [], [])
            self.assertEqual(raw.read_text(encoding="utf-8"), '{"raw": true}\n')
            self.assertEqual(json.loads(priority_json.read_text(encoding="utf-8"))["artifact_type"], "booktext.review_priority")


if __name__ == "__main__":
    unittest.main()
