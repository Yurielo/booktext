"""Machine review endpoints are a thin, lazy bridge to the production workspace."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class MachineReviewProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        (self.root / "pages").mkdir(); (self.root / "pages" / "p1.png").write_bytes(b"png")
        write_json(self.root / "book.json", {"pages": [
            {"page": 1, "text": "alpha", "page_image": "pages/p1.png", "tables": {"tables": []}},
            {"page": 319, "text": "table", "tables": {"tables": [{"status": "fallback_review"}]}},
        ]})
        write_json(self.root / "review_priority.json", {"schema_version": "1.0", "artifact_type": "booktext.review_priority", "counts": {}, "items": [
            {"item_id": "high", "page": 1, "priority": "HIGH", "item_type": "ocr_qa", "surya": "alpha", "vision": "ALPHA", "reasons": ["numeric"], "signals": {"vision_confidence": .9}},
            {"item_id": "fallback-319", "page": 319, "priority": "INFO", "item_type": "table_fallback_review", "reasons": ["not_verified_structured_data"], "signals": {}},
        ]})

    def tearDown(self): self.temp.cleanup()

    def call(self, *arguments):
        result = subprocess.run(["python3", "booktext.py", "machine", *arguments], cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(events[-1]["event_type"], "job_completed")
        return events[-1]["details"]

    def test_lazy_item_query_exposes_safe_asset_and_core_status(self):
        payload = self.call("review-item", str(self.root), "--index", "0")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["item"]["item_id"], "high")
        self.assertEqual(payload["item"]["source_asset"]["page_image"], "pages/p1.png")
        self.assertFalse(payload["item"]["source_asset"]["crop_available"])
        self.assertEqual(payload["progress"]["unresolved_high"], 1)

    def test_decision_and_fallback_defer_round_trip_through_core(self):
        self.call("decision", str(self.root), "--item", "high", "--action", "keep_surya", "--replace")
        payload = self.call("review-item", str(self.root), "--item", "high")
        self.assertEqual(payload["item"]["status"]["status"], "resolved_no_change")
        self.call("decision", str(self.root), "--item", "fallback-319", "--action", "defer", "--replace")
        fallback = self.call("review-item", str(self.root), "--item", "fallback-319")["item"]
        self.assertEqual(fallback["status"]["status"], "deferred")
        self.assertEqual(fallback["source_asset"]["crop"], None)
        decisions = json.loads((self.root / "review_decisions.json").read_text(encoding="utf-8"))
        self.assertEqual({entry["action"] for entry in decisions["decisions"]}, {"keep_surya", "defer"})

    def test_clusters_are_core_owned_and_members_remain_visible(self):
        payload = self.call("review-clusters", str(self.root), "--index", "0")
        self.assertGreaterEqual(payload["count"], 1)
        cluster = payload["cluster"]
        self.assertIn("members", cluster)
        self.assertIn("decision_statistics", cluster)
        self.assertIn("batch_eligible", cluster)


if __name__ == "__main__": unittest.main()
