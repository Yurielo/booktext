import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import machine_protocol as protocol
from tools import sequential_pipeline


def events(call):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = call()
    return code, [json.loads(line) for line in stream.getvalue().splitlines() if line]


class SequentialPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "out"
        self.source = Path(self.tmp.name) / "source.pdf"
        self.source.write_bytes(b"not an OCR fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def definition(self, *, translation=False, review="skip_human_review", formats=("docx",)):
        return sequential_pipeline.default_definition(
            review_policy=review,
            translation={"source_language": "en", "target_language": "ru", "provider": "ollama", "model": "qwen3:8b"} if translation else None,
            export_formats=list(formats),
        )

    def fixture_processed(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "book.json").write_text(json.dumps({"pages": [{"page": 1, "text": "A source paragraph.", "page_image": None, "visuals": [], "tables": {"tables": []}}]}), encoding="utf-8")
        (self.root / "review_priority.json").write_text(json.dumps({"schema_version": "1.0", "artifact_type": "booktext.review_priority", "counts": {"HIGH": 0, "MEDIUM": 0}, "items": []}), encoding="utf-8")
        (self.root / "review.json").write_text(json.dumps({"pages": []}), encoding="utf-8")
        (self.root / "run_summary.json").write_text(json.dumps({"pages": {"processed": 1}}), encoding="utf-8")

    def test_valid_definition_and_invalid_orders(self):
        self.assertTrue(sequential_pipeline.validate_definition(self.definition(translation=True))["valid"])
        broken = self.definition(); broken["stages"].reverse()
        with self.assertRaisesRegex(sequential_pipeline.PipelineError, "begin with PROCESS"):
            sequential_pipeline.validate_definition(broken)
        translation = self.definition(translation=True)
        translation["stages"][4]["config"]["target_language"] = "en"
        with self.assertRaisesRegex(sequential_pipeline.PipelineError, "distinct"):
            sequential_pipeline.validate_definition(translation)

    def test_create_is_durable_idempotent_and_machine_accessible(self):
        definition = self.definition()
        state, created = sequential_pipeline.create(self.root, source=self.source, definition=definition)
        again, created_again = sequential_pipeline.create(self.root, source=self.source, definition=definition)
        self.assertTrue(created); self.assertFalse(created_again)
        self.assertEqual(state["pipeline_id"], again["pipeline_id"])
        self.assertEqual("PLANNED", again["status"])
        code, payload = events(lambda: protocol.main(["pipeline-status", str(self.root)]))
        self.assertEqual(0, code)
        self.assertEqual("PLANNED", payload[-1]["details"]["pipeline"]["status"])

    def test_skip_review_final_build_cdm_and_original_export(self):
        definition = self.definition(formats=("markdown", "html"))
        state, _ = sequential_pipeline.create(self.root, source=self.source, definition=definition)
        protocol.ensure_job(self.root, status="planned", source=[str(self.source)])
        def processed(*_args, **_kwargs):
            self.fixture_processed(); return 0
        def exported_result(*_args, **_kwargs):
            exports = self.root / "exports"; exports.mkdir(exist_ok=True)
            (exports / "export_manifest.json").write_text(json.dumps({"validation": {"cdm": "PASS"}}), encoding="utf-8")
            return {"generated": {"markdown": {}, "html": {}}}
        with mock.patch.object(protocol, "_run_existing", side_effect=processed), \
             mock.patch.object(protocol.export_document, "export_document", side_effect=exported_result) as exported:
            code, payload = events(lambda: protocol._run_pipeline(self.root))
        self.assertEqual(0, code)
        self.assertEqual("pipeline_completed", payload[-2]["event_type"])
        self.assertEqual("COMPLETED", sequential_pipeline._load(self.root)["status"])
        exported.assert_called_once_with(self.root.resolve(), ["markdown", "html"])
        self.assertTrue((self.root / "book_document.json").exists())
        self.assertTrue((self.root / "book_final.txt").exists())
        self.assertEqual(["process", "review", "final-build", "cdm", "export"], sequential_pipeline._load(self.root)["completed_stage_ids"])

    def test_translation_export_uses_translation_not_original_and_resume_skips_completed(self):
        definition = self.definition(translation=True)
        sequential_pipeline.create(self.root, source=self.source, definition=definition)
        protocol.ensure_job(self.root, status="planned", source=[str(self.source)])
        calls = []
        def processed(*_args, **_kwargs):
            calls.append("process"); self.fixture_processed(); return 0
        def translated_result(*_args, **_kwargs):
            translations = self.root / "translations"; translations.mkdir(exist_ok=True)
            (translations / "en-ru.json").write_text(json.dumps({"artifact_type": "booktext.translation", "validation": {"valid": True, "stale": False}}), encoding="utf-8")
            return {"validation": {"valid": True, "stale": False}}
        def translation_exported(*_args, **_kwargs):
            exports = self.root / "translations" / "exports" / "en-ru"; exports.mkdir(parents=True, exist_ok=True)
            (exports / "export_manifest.json").write_text(json.dumps({"validation": {"cdm": "PASS"}}), encoding="utf-8")
            return {"generated": {"docx": {}}}
        with mock.patch.object(protocol, "_run_existing", side_effect=processed), \
             mock.patch.object(protocol.translation, "translate_document", side_effect=translated_result) as translated, \
             mock.patch.object(protocol.export_document, "export_translation", side_effect=translation_exported) as exported:
            code, _ = events(lambda: protocol._run_pipeline(self.root))
            self.assertEqual(0, code)
            code, payload = events(lambda: protocol._run_pipeline(self.root, resume=True))
        self.assertEqual(0, code); self.assertIn("pipeline_resumed", [event["event_type"] for event in payload])
        self.assertEqual(["process"], calls)
        translated.assert_called_once()
        exported.assert_called_once_with(self.root.resolve(), ["docx"], source_language="en", target_language="ru")

    def test_artifact_map_discovers_namespaced_translation_export(self):
        manifest = self.root / "translations" / "exports" / "en-ru" / "export_manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
        self.assertEqual("translations/exports/en-ru/export_manifest.json", protocol.artifact_map(self.root)["exports"]["path"])
        self.assertTrue(protocol.artifact_map(self.root)["exports"]["exists"])

    def test_review_required_pauses_without_mutating_raw_evidence_or_downstream(self):
        definition = self.definition(review="review_when_required")
        sequential_pipeline.create(self.root, source=self.source, definition=definition)
        protocol.ensure_job(self.root, status="planned", source=[str(self.source)])
        def processed(*_args, **_kwargs):
            self.fixture_processed()
            priority = json.loads((self.root / "review_priority.json").read_text(encoding="utf-8")); priority["counts"]["HIGH"] = 1
            (self.root / "review_priority.json").write_text(json.dumps(priority), encoding="utf-8")
            return 0
        with mock.patch.object(protocol, "_run_existing", side_effect=processed):
            code, payload = events(lambda: protocol._run_pipeline(self.root))
        self.assertEqual(0, code)
        self.assertEqual("pipeline_paused", payload[-1]["event_type"])
        state = sequential_pipeline._load(self.root)
        self.assertEqual("PAUSED", state["status"])
        self.assertFalse((self.root / "book_final.txt").exists())
        self.assertFalse((self.root / "review_decisions.json").exists())

    def test_failure_blocks_downstream_and_cancel_is_resumable(self):
        definition = self.definition()
        sequential_pipeline.create(self.root, source=self.source, definition=definition)
        protocol.ensure_job(self.root, status="planned", source=[str(self.source)])
        def processed(*_args, **_kwargs): self.fixture_processed(); return 0
        with mock.patch.object(protocol, "_run_existing", side_effect=processed), mock.patch.object(protocol, "_final_build", side_effect=ValueError("bad final")):
            code, payload = events(lambda: protocol._run_pipeline(self.root))
        self.assertEqual(protocol.EXIT_CODES["validation_failure"], code)
        self.assertEqual("stage_failed", payload[-2]["event_type"])
        state = sequential_pipeline._load(self.root)
        self.assertEqual("FAILED", state["status"])
        self.assertNotIn("cdm", state["completed_stage_ids"])
        item = next(stage for stage in state["stages"] if stage["stage_id"] == "final-build")
        sequential_pipeline.mark_paused(self.root, state, item, code="cancelled", message="test", cancellation=True)
        self.assertEqual("PAUSED", sequential_pipeline._load(self.root)["status"])

    def test_post_execution_validation_failure_is_durably_failed_and_blocks_downstream(self):
        sequential_pipeline.create(self.root, source=self.source, definition=self.definition())
        protocol.ensure_job(self.root, status="planned", source=[str(self.source)])
        # The producer returns normally but creates none of PROCESS's required
        # evidence.  This must not leave a misleading VALIDATING stage.
        with mock.patch.object(protocol, "_run_existing", return_value=0):
            code, payload = events(lambda: protocol._run_pipeline(self.root))
        self.assertEqual(protocol.EXIT_CODES["validation_failure"], code)
        self.assertEqual("stage_failed", payload[-2]["event_type"])
        state = sequential_pipeline._load(self.root)
        self.assertEqual("FAILED", state["status"])
        process = state["stages"][0]
        self.assertEqual("FAILED", process["status"])
        self.assertEqual("validation_failure", process["failure"]["code"])
        self.assertFalse((self.root / "book_final.txt").exists())


if __name__ == "__main__":
    unittest.main()
