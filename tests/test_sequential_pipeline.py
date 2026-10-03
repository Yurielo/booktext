import contextlib
import fcntl
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
            translation={"source_language": "en", "target_language": "ru", "provider": "ollama", "model": "translategemma:4b"} if translation else None,
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
        unsupported_schema = self.definition(); unsupported_schema["schema_version"] = "2.0"
        with self.assertRaisesRegex(sequential_pipeline.PipelineError, "unsupported schema"):
            sequential_pipeline.validate_definition(unsupported_schema)
        broken = self.definition(); broken["stages"].reverse()
        with self.assertRaisesRegex(sequential_pipeline.PipelineError, "begin with PROCESS"):
            sequential_pipeline.validate_definition(broken)
        translation = self.definition(translation=True)
        translation["stages"][4]["config"]["target_language"] = "en"
        with self.assertRaisesRegex(sequential_pipeline.PipelineError, "distinct"):
            sequential_pipeline.validate_definition(translation)
        translation = self.definition(translation=True)
        translation["stages"][-1]["config"].pop("document")
        with self.assertRaisesRegex(sequential_pipeline.PipelineError, "explicitly choose"):
            sequential_pipeline.validate_definition(translation)
        unsupported = self.definition(); unsupported["stages"][-1]["config"]["formats"] = ["docx", "exe"]
        with self.assertRaisesRegex(sequential_pipeline.PipelineError, "supported formats"):
            sequential_pipeline.validate_definition(unsupported)

    def test_legacy_translation_pipeline_migrates_only_after_explicit_command(self):
        definition = self.definition(translation=True)
        definition["stages"][4]["config"]["model"] = "qwen3:8b"
        self.root.mkdir()
        state = {"schema_version": sequential_pipeline.SCHEMA_VERSION, "artifact_type": sequential_pipeline.ARTIFACT_TYPE,
                 "pipeline_id": "old", "source": str(self.source), "definition": definition, "status": "PAUSED",
                 "current_stage_id": "translate", "stages": [{**stage, "status": "COMPLETE" if stage["type"] in {"PROCESS", "REVIEW_POLICY", "FINAL_BUILD", "CDM"} else "PENDING", "artifact_refs": {}} for stage in definition["stages"]],
                 "completed_stage_ids": ["process", "review", "final-build", "cdm"]}
        sequential_pipeline.save(self.root, state)
        with self.assertRaisesRegex(sequential_pipeline.PipelineError, "MODEL_RETIRED"):
            sequential_pipeline.validate_definition(sequential_pipeline._load(self.root)["definition"])
        migrated, changed = sequential_pipeline.migrate_legacy_translation(self.root)
        self.assertTrue(changed)
        self.assertEqual("qwen3:8b", migrated["migration_history"][0]["previous_stage"]["config"]["model"])
        self.assertEqual("translategemma:4b", migrated["stages"][4]["config"]["model"])
        self.assertTrue(sequential_pipeline.validate_definition(migrated["definition"])["valid"])
        _, changed_again = sequential_pipeline.migrate_legacy_translation(self.root)
        self.assertFalse(changed_again)

    def test_create_is_durable_idempotent_and_machine_accessible(self):
        definition = self.definition()
        state, created = sequential_pipeline.create(self.root, source=self.source, definition=definition)
        again, created_again = sequential_pipeline.create(self.root, source=self.source, definition=definition)
        self.assertTrue(created); self.assertFalse(created_again)
        self.assertEqual(state["pipeline_id"], again["pipeline_id"])
        other_source = self.source.with_name("other.pdf"); other_source.write_bytes(b"different")
        with self.assertRaisesRegex(sequential_pipeline.PipelineError, "source does not match"):
            sequential_pipeline.create(self.root, source=other_source, definition=definition)
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
            generated = {}
            for fmt in ("markdown", "html"):
                output = exports / f"book.{'md' if fmt == 'markdown' else fmt}"
                output.write_text("export fixture", encoding="utf-8")
                generated[fmt] = {"path": str(output.relative_to(self.root)), "valid": True,
                                  "sha256": protocol.review_decisions.sha256_file(output)}
            (exports / "export_manifest.json").write_text(json.dumps({"validation": {"cdm": "PASS"},
                "cdm": {"sha256": protocol.review_decisions.sha256_file(self.root / "book_document.json")},
                "generated": generated}), encoding="utf-8")
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
            document = exports / "book-en-ru.docx"; document.write_bytes(b"translated DOCX fixture")
            (exports / "export_manifest.json").write_text(json.dumps({
                "validation": {"cdm": "PASS"},
                "cdm": {"sha256": protocol.review_decisions.sha256_file(self.root / "book_document.json")},
                "translation": {"path": "translations/en-ru.json"},
                "generated": {"docx": {"path": "translations/exports/en-ru/book-en-ru.docx", "valid": True,
                                       "sha256": protocol.review_decisions.sha256_file(document)}},
            }), encoding="utf-8")
            return {"generated": {"docx": {}}}
        with mock.patch.object(protocol, "_run_existing", side_effect=processed), \
             mock.patch.object(protocol.translation, "translate_document", side_effect=translated_result) as translated, \
             mock.patch.object(protocol.translation, "validate_translation", return_value={"valid": True, "stale": False}), \
             mock.patch.object(protocol.export_document, "export_translation", side_effect=translation_exported) as exported:
            code, _ = events(lambda: protocol._run_pipeline(self.root))
            self.assertEqual(0, code)
            code, payload = events(lambda: protocol._run_pipeline(self.root, resume=True))
            translated_docx = self.root / "translations" / "exports" / "en-ru" / "book-en-ru.docx"
            translated_docx.write_bytes(b"corrupt")
            code_after_stale, _ = events(lambda: protocol._run_pipeline(self.root, resume=True))
        self.assertEqual(0, code); self.assertIn("pipeline_resumed", [event["event_type"] for event in payload])
        self.assertEqual(0, code_after_stale)
        self.assertEqual(["process"], calls)
        translated.assert_called_once()
        self.assertEqual(2, exported.call_count)
        exported.assert_called_with(self.root.resolve(), ["docx"], source_language="en", target_language="ru")

    def test_translation_quality_signal_pauses_before_export(self):
        sequential_pipeline.create(self.root, source=self.source, definition=self.definition(translation=True))
        protocol.ensure_job(self.root, status="planned", source=[str(self.source)])
        def processed(*_args, **_kwargs):
            self.fixture_processed(); return 0
        def translated(*_args, **_kwargs):
            path = self.root / "translations" / "en-ru.json"
            path.parent.mkdir(); path.write_text("{}", encoding="utf-8")
            return {"validation": {"valid": True}}
        with mock.patch.object(protocol, "_run_existing", side_effect=processed), \
             mock.patch.object(protocol.translation, "translate_document", side_effect=translated), \
             mock.patch.object(protocol.translation, "validate_translation", return_value={"valid": True, "stale": False, "needs_numeric_review": 1}), \
             mock.patch.object(protocol.export_document, "export_translation") as export:
            code, payload = events(lambda: protocol._run_pipeline(self.root))
        self.assertEqual(0, code)
        self.assertEqual("TRANSLATION_REVIEW_REQUIRED", payload[-1]["code"])
        self.assertEqual("PAUSED", sequential_pipeline._load(self.root)["status"])
        export.assert_not_called()

    def test_concurrent_start_is_rejected_without_changing_pipeline_state(self):
        sequential_pipeline.create(self.root, source=self.source, definition=self.definition())
        lock_path = self.root / ".booktext_pipeline.lock"
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(protocol.MachineError) as captured:
                protocol._run_pipeline(self.root)
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual("PIPELINE_BUSY", captured.exception.code)
        self.assertEqual("PLANNED", sequential_pipeline._load(self.root)["status"])

    def test_cancel_command_signals_active_worker_without_racing_its_checkpoint(self):
        sequential_pipeline.create(self.root, source=self.source, definition=self.definition())
        protocol.ensure_job(self.root, status="running", source=[str(self.source)])
        with mock.patch.object(protocol, "_signal_active_pipeline_worker", return_value=True) as signal_worker:
            code, payload = events(lambda: protocol.main(["pipeline-cancel", str(self.root)]))
        self.assertEqual(0, code)
        self.assertEqual("job_completed", payload[-1]["event_type"])
        self.assertEqual("PLANNED", sequential_pipeline._load(self.root)["status"])
        signal_worker.assert_called_once()
        self.assertEqual(self.root.resolve(), signal_worker.call_args.args[0].resolve())

    def test_project_history_projection_exposes_current_stage_direction_and_export(self):
        state, _ = sequential_pipeline.create(self.root, source=self.source, definition=self.definition(translation=True))
        protocol.ensure_job(self.root, status="planned", source=[str(self.source)])
        translation_stage = next(item for item in state["stages"] if item["type"] == "TRANSLATE")
        sequential_pipeline.mark_paused(self.root, state, translation_stage, code="TRANSLATION_REVIEW_REQUIRED", message="Review translation")
        project = protocol.inspect_output(self.root)["project"]
        self.assertEqual("PAUSED", project["pipeline_status"])
        self.assertEqual("TRANSLATE", project["pipeline_current_stage_type"])
        self.assertEqual("TRANSLATION_REVIEW_REQUIRED", project["pipeline_failure_code"])
        self.assertEqual("EN → RU", project["pipeline_translation_direction"])
        self.assertEqual(["docx"], project["pipeline_export_formats"])

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
