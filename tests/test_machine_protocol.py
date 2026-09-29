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

    @mock.patch("tools.machine_protocol.export_document.export_document", return_value={"generated": {"docx": {}}})
    def test_machine_export_passes_only_explicit_selected_formats(self, exported):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            code, payload = events(lambda: protocol.main(["export", str(root), "--format", "docx", "--format", "pdf_image"]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            exported.assert_called_once_with(root, ["docx", "pdf_image"])

    def test_document_preview_is_bounded_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            code, _payload = events(lambda: protocol.main(["cdm", str(root)]))
            self.assertEqual(0, code)
            before = (root / "book_document.json").read_bytes()
            code, payload = events(lambda: protocol.main(["document-preview", str(root), "--index", "0", "--limit", "1"]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            result = payload[-1]["details"]["result"]
            self.assertEqual(1, len(result["blocks"]))
            self.assertEqual(before, (root / "book_document.json").read_bytes())

    def test_document_preview_exposes_only_verified_structured_table_cells(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            (root / "tables").mkdir()
            (root / "tables" / "page-0001-table-01.json").write_text(json.dumps({"grid": [["H"], ["1"]]}), encoding="utf-8")
            (root / "tables" / "page-0001-table-01.tsv").write_text("H\n1\n", encoding="utf-8")
            (root / "tables" / "page-0001-table-01.html").write_text("<table><tr><td>H</td></tr></table>", encoding="utf-8")
            book = json.loads((root / "book.json").read_text(encoding="utf-8"))
            book["pages"][0]["tables"] = {"tables": [{"index": 1, "status": "HTML_TABLE", "rows": 2, "columns": 1, "json": "page-0001-table-01.json", "tsv": "page-0001-table-01.tsv", "html": "page-0001-table-01.html"}]}
            (root / "book.json").write_text(json.dumps(book), encoding="utf-8")
            (root / "surya" / "surya-input").mkdir(parents=True)
            (root / "surya" / "surya-input" / "results.json").write_text(json.dumps({"pages": [{"page": 1, "blocks": [{"label": "Table", "reading_order": 1}]}]}), encoding="utf-8")
            code, payload = events(lambda: protocol.main(["cdm", str(root)]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            code, payload = events(lambda: protocol.main(["document-preview", str(root), "--index", "0", "--limit", "10"]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            table = next(block for block in payload[-1]["details"]["result"]["blocks"] if block["type"] == "table")
            self.assertEqual([["H"], ["1"]], table["table_preview"])

    @mock.patch("tools.machine_protocol.translation.translate_document", return_value={"path": "translations/en-ru.json", "validation": {"valid": True}})
    def test_translation_uses_versioned_machine_operation_without_ocr(self, translate):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            code, payload = events(lambda: protocol.main(["translation", str(root), "--source-language", "en", "--target-language", "ru", "--model", "qwen3:8b"]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            self.assertEqual("translation", payload[-1]["phase"])
            translate.assert_called_once()
            kwargs = translate.call_args.kwargs
            self.assertEqual("qwen3:8b", kwargs["model"])
            self.assertEqual("en", kwargs["source_language"])
            self.assertEqual("ru", kwargs["target_language"])
            self.assertEqual("30s", kwargs["keep_alive"])

    def test_decision_and_final_build_use_existing_decision_semantics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            code, payload = events(lambda: protocol.main(["decision", str(root), "--item", "item-1", "--action", "accept_vision"]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            code, payload = events(lambda: protocol.main(["final-build", str(root)]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            self.assertTrue((root / "book_final.txt").is_file())
            self.assertIn("replacement", (root / "book_final.txt").read_text(encoding="utf-8"))

    def test_final_build_without_prior_decisions_creates_empty_source_of_truth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            code, payload = events(lambda: protocol.main(["final-build", str(root)]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            self.assertTrue((root / "review_decisions.json").is_file())
            self.assertTrue((root / "book_final.txt").is_file())

    def test_confirmed_raw_surya_baseline_is_explicit_and_uses_no_ocr_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            code, payload = events(lambda: protocol.main(["baseline-preview", str(root)]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            preview = payload[-1]["details"]["result"]
            self.assertTrue(preview["requires_confirmation"])
            self.assertFalse((root / "review_decisions.json").exists())
            with mock.patch("subprocess.run", side_effect=AssertionError("must not invoke OCR/model")), mock.patch("subprocess.Popen", side_effect=AssertionError("must not invoke OCR/model")):
                code, payload = events(lambda: protocol.main(["baseline-confirm", str(root), "--confirmed"]))
                self.assertEqual(0, code); self.assert_protocol(payload)
                self.assertTrue(payload[-1]["details"]["result"]["applied"])
                code, payload = events(lambda: protocol.main(["final-build", str(root)]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            self.assertIn("A source sentence.", (root / "book_final.txt").read_text(encoding="utf-8"))

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

    def test_cleanup_operation_reports_safe_chunk_lifecycle_result(self):
        from tools import chunk_lifecycle
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = chunk_lifecycle.create_or_load_plan(root, job_id="job-clean", source_fp="source", operation="surya_ocr", page_numbers=[1], options={"engine": "surya"})
            chunk = plan["chunks"][0]
            chunk_lifecycle.begin_attempt(root, plan, chunk)
            chunk_lifecycle.commit_checkpoint(root, plan, chunk, {"page-0001": [{"text": "source"}]}, duration_seconds=.1)
            chunk_lifecycle.mark_assembly(root, plan, complete=True, validation={"pages": "PASS"})
            code, payload = events(lambda: protocol.main(["cleanup", str(root)]))
            self.assertEqual(0, code); self.assert_protocol(payload)
            self.assertEqual("cleanup_started", payload[0]["event_type"])
            self.assertEqual("cleanup_completed", payload[1]["event_type"])
            self.assertTrue(payload[1]["details"]["cleanup_allowed"])

    def test_protocol_fixtures_are_valid_jsonl(self):
        fixture_dir = Path(__file__).parent / "fixtures" / "machine_protocol"
        for fixture in fixture_dir.glob("*.jsonl"):
            with self.subTest(fixture=fixture.name):
                self.assert_protocol([json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines() if line])
