import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import machine_protocol as protocol


def events(call):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = call()
    return code, [json.loads(line) for line in output.getvalue().splitlines() if line]


class MachineProtocolTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        (root / "book.json").write_text(json.dumps({"pages": [{"page": 1, "text": "A source sentence.", "page_image": None, "visuals": [], "tables": {"tables": []}}]}), encoding="utf-8")
        (root / "review_priority.json").write_text(json.dumps({"schema_version": "1.0", "artifact_type": "booktext.review_priority", "counts": {"HIGH": 1, "MEDIUM": 0}, "items": [{"item_id": "item-1", "page": 1, "priority": "HIGH", "item_type": "ocr_qa", "surya": "source", "vision": "replacement", "reasons": [], "signals": {}}]}), encoding="utf-8")
        return root

    def assert_protocol(self, payload):
        self.assertTrue(payload)
        for event in payload:
            self.assertEqual("1.0", event["protocol_version"])
            self.assertIn(event["event_type"], protocol.EVENT_TYPES)
            self.assertIn("job_id", event)
            self.assertIn("details", event)

    def test_preflight_stdout_is_jsonl_and_optional_dependencies_do_not_block(self):
        code, payload = events(lambda: protocol.main(["preflight", "--operation", "export"]))
        self.assertEqual(0, code); self.assert_protocol(payload)
        self.assertEqual("job_completed", payload[-1]["event_type"])
        self.assertIn("optional", payload[-1]["details"]["dependencies"])

    def test_cdm_export_and_inspect_preserve_persistent_job_and_artifact_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            code, payload = events(lambda: protocol.main(["cdm", str(root)]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            self.assertTrue((root / "book_document.json").is_file())
            code, payload = events(lambda: protocol.main(["export", str(root), "--format", "markdown", "--format", "html"]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            self.assertTrue((root / "exports" / "export_manifest.json").is_file())
            code, payload = events(lambda: protocol.main(["inspect", str(root)]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            result = payload[-1]["details"]
            self.assertEqual(payload[-1]["job_id"], result["job"]["job_id"])
            self.assertTrue(result["artifacts"]["cdm"]["exists"])
            self.assertTrue(result["artifacts"]["exports"]["exists"])
            self.assertEqual(1, result["review"]["unresolved_priority_counts"]["HIGH"])

    def test_decision_and_final_build_use_existing_decision_semantics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            code, payload = events(lambda: protocol.main(["decision", str(root), "--item", "item-1", "--action", "accept_vision"]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            code, payload = events(lambda: protocol.main(["final-build", str(root)]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            self.assertTrue((root / "book_final.txt").is_file())
            self.assertIn("replacement", (root / "book_final.txt").read_text(encoding="utf-8"))

    def test_failure_and_cancellation_event_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "missing"
            code, payload = events(lambda: protocol.main(["inspect", str(root)]))
            self.assertEqual(protocol.EXIT_CODES["invalid_input"], code); self.assert_protocol(payload)
            self.assertEqual("job_failed", payload[-1]["event_type"])
            root.mkdir()
            with mock.patch("tools.machine_protocol._handle", side_effect=KeyboardInterrupt):
                code, payload = events(lambda: protocol.main(["cdm", str(root)]))
            self.assertEqual(protocol.EXIT_CODES["cancelled"], code); self.assert_protocol(payload)
            self.assertEqual("job_cancelled", payload[-1]["event_type"])
            self.assertEqual("cancelled", protocol._load(root / protocol.JOB_FILENAME)["status"])

    def test_protocol_fixtures_are_valid_jsonl(self):
        fixture_dir = Path(__file__).parent / "fixtures" / "machine_protocol"
        for fixture in fixture_dir.glob("*.jsonl"):
            with self.subTest(fixture=fixture.name):
                self.assert_protocol([json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines() if line])
