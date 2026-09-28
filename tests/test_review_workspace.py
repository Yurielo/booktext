import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from tools import review_workspace


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ReviewWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "pages").mkdir()
        (self.root / "pages" / "p1.png").write_bytes(b"png")
        self.book = {"pages": [
            {"page": 1, "text": "alpha <script>alert(1)</script> beta", "page_image": "pages/p1.png", "visuals": [], "tables": {"tables": []}},
            {"page": 2, "text": "gamma", "visuals": [], "tables": {"tables": [{"index": 1, "status": "fallback_review"}]}},
        ]}
        self.priority = {"schema_version": "1.0", "artifact_type": "booktext.review_priority", "counts": {}, "items": [
            {"item_id": "low", "page": 1, "priority": "LOW", "item_type": "ocr_qa", "surya": "beta", "vision": "BETA", "reasons": ["text"], "signals": {"vision_confidence": .8, "vision_bbox": [0.1, 0.2, 0.3, 0.4]}},
            {"item_id": "high", "page": 1, "priority": "HIGH", "item_type": "ocr_qa", "surya": "alpha", "vision": "ALPHA", "reasons": ["numeric"], "signals": {"surya_bbox": [0.1, 0.2, 0.3, 0.4]}},
            {"item_id": "medium", "page": 2, "priority": "MEDIUM", "item_type": "ocr_qa", "surya": "gamma", "vision": "GAMMA", "reasons": ["text"], "signals": {}},
            {"item_id": "table", "page": 2, "priority": "INFO", "item_type": "table_fallback_review", "reasons": ["not_verified_structured_data"], "signals": {}},
        ]}
        write_json(self.root / "book.json", self.book); write_json(self.root / "review_priority.json", self.priority)
        self.workspace = review_workspace.ReviewWorkspace(self.root)

    def tearDown(self): self.temp.cleanup()

    def test_validation_loopback_ordering_filters_and_lazy_item_payload(self):
        with self.assertRaises(ValueError): review_workspace.create_server(self.root, host="0.0.0.0")
        self.assertEqual(self.workspace.filtered_ids({})[:3], ["high", "medium", "low"])
        self.assertEqual(self.workspace.filtered_ids({"status": "unresolved", "priority": "HIGH"}), ["high"])
        self.assertEqual(self.workspace.filtered_ids({"page": "2"}), ["medium", "table"])
        payload = self.workspace.item_payload("high")
        self.assertNotIn("gamma", json.dumps(payload))
        self.assertTrue(payload["image_available"])
        self.assertEqual(payload["source_asset"]["location_kind"], "surya_text_block")
        self.assertEqual(payload["source_asset"]["crop"], [0.1, 0.2, 0.3, 0.4])
        self.assertEqual(payload["source_asset"]["location_accuracy"], "block")

    def test_legacy_vision_bbox_is_flipped_to_the_source_page_coordinate_space(self):
        payload = self.workspace.item_payload("low")
        self.assertEqual(payload["source_asset"]["location_kind"], "vision_word")
        self.assertEqual(payload["source_asset"]["location_accuracy"], "exact")
        self.assertEqual(payload["source_asset"]["coordinate_space"], "source_page_normalized_top_left")
        self.assertEqual(payload["source_asset"]["crop"], [0.1, 0.6, 0.3, 0.8])

    def test_unknown_bbox_coordinate_space_fails_closed_without_a_highlight(self):
        self.priority["items"][0]["signals"]["vision_bbox_coordinate_space"] = "unknown"
        write_json(self.root / "review_priority.json", self.priority)
        payload = review_workspace.ReviewWorkspace(self.root).item_payload("low")
        self.assertIsNone(payload["source_asset"]["crop"])
        self.assertEqual(payload["source_asset"]["location_accuracy"], "unavailable")

    def test_decisions_persist_atomically_across_restart_and_progress_updates(self):
        self.workspace.record({"item_id": "high", "action": "keep_surya"})
        self.workspace.record({"item_id": "medium", "action": "accept_vision"})
        self.workspace.record({"item_id": "table", "action": "defer"})
        decision_path = self.root / "review_decisions.json"
        self.assertEqual(json.loads(decision_path.read_text())["artifact_type"], "booktext.review_decisions")
        restarted = review_workspace.ReviewWorkspace(self.root)
        self.assertEqual(restarted.status_for("high")["status"], "resolved_no_change")
        self.assertEqual(restarted.status_for("medium")["status"], "resolved_changed")
        self.assertEqual(restarted.status_for("table")["status"], "deferred")
        self.assertEqual(restarted.progress()["unresolved_high"], 0)

    def test_invalid_action_and_table_cell_invention_fail_safely(self):
        with self.assertRaisesRegex(ValueError, "table_fallback"):
            self.workspace.record({"item_id": "table", "action": "accept_vision"})
        with self.assertRaisesRegex(ValueError, "manual"):
            self.workspace.record({"item_id": "high", "action": "manual"})

    def test_final_build_uses_existing_layer_without_subprocess(self):
        self.workspace.record({"item_id": "medium", "action": "manual", "replacement_text": "GAMMA"})
        with patch("subprocess.Popen", side_effect=AssertionError("no OCR/model")), patch("subprocess.run", side_effect=AssertionError("no OCR/model")):
            report = self.workspace.build_final()
        self.assertEqual(report["applied_text_changes"], 1)
        self.assertTrue((self.root / "book_final.txt").exists())

    def test_http_path_traversal_rejected_and_html_uses_text_content(self):
        server = review_workspace.create_server(self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(HTTPError) as error: urlopen(base + "/asset/page/../book.json")
            self.assertEqual(error.exception.code, 404); error.exception.close()
            page = urlopen(base + "/").read().decode()
            self.assertIn("textContent", page); self.assertNotIn("innerHTML", page)
            payload = json.loads(urlopen(base + "/api/item?index=0").read())
            self.assertNotIn("<script>", json.dumps(payload))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_http_decision_autosaves(self):
        server = review_workspace.create_server(self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/api/decision", data=json.dumps({"item_id": "high", "action": "ignore"}).encode(), method="POST")
            response = json.loads(urlopen(request).read())
            self.assertEqual(response["status"]["status"], "resolved_no_change")
            self.assertTrue((self.root / "review_decisions.json").exists())
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__": unittest.main()
