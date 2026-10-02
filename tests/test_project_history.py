import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools import machine_protocol as protocol
from tools import project_history


def machine(call):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = call()
    return code, [json.loads(line) for line in output.getvalue().splitlines() if line]


class ProjectHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "example_book"
        self.history = Path(self.temporary.name) / "history.json"
        self.source = Path(self.temporary.name) / "source.pdf"
        self.source.write_bytes(b"source")
        self.root.mkdir()
        (self.root / "book.json").write_text(json.dumps({"pages": [{"page": 1, "text": "Text", "page_image": None, "visuals": [], "tables": {"tables": []}}]}), encoding="utf-8")
        (self.root / "review_priority.json").write_text(json.dumps({"counts": {"HIGH": 2, "MEDIUM": 0}, "items": []}), encoding="utf-8")
        self.state = protocol.ensure_job(self.root, status="completed", source=[str(self.source)])

    def tearDown(self):
        self.temporary.cleanup()

    def _history_command(self, *extra):
        return machine(lambda: protocol.main(["project-history", "--history-path", str(self.history), *extra]))

    def test_new_project_survives_relaunch_ordering_and_no_duplicate(self):
        first = project_history.record(self.root, self.state, history_path=self.history)
        second = project_history.record(self.root, self.state, history_path=self.history)
        self.assertEqual(first["project_id"], second["project_id"])
        code, events = self._history_command()
        self.assertEqual(0, code)
        projects = events[-1]["details"]["projects"]
        self.assertEqual(1, len(projects))
        self.assertEqual("REVIEW_REQUIRED", projects[0]["state"])
        self.assertEqual(1, projects[0]["page_count"])
        # A fresh read simulates relaunch and must use the durable manifest.
        code, events = self._history_command()
        self.assertEqual(first["project_id"], events[-1]["details"]["projects"][0]["project_id"])

    def test_history_migrates_only_explicit_known_output_and_never_runs_ocr(self):
        before = (self.root / "book.json").read_bytes()
        code, events = self._history_command("--known-output", str(self.root))
        self.assertEqual(0, code)
        self.assertEqual(1, len(events[-1]["details"]["projects"]))
        self.assertEqual(before, (self.root / "book.json").read_bytes())
        self.assertFalse((self.root / "surya").exists())

    def test_unverified_known_directory_is_controlled_without_creating_job_or_artifacts(self):
        empty = Path(self.temporary.name) / "stale_cloud_placeholder"
        empty.mkdir()
        code, events = machine(lambda: protocol.main(["inspect", str(empty), "--record-activity"]))
        self.assertEqual(0, code)
        self.assertEqual("PROJECT_FILES_UNAVAILABLE", events[-1]["details"]["project"]["availability"])
        self.assertFalse((empty / ".booktext_job.json").exists())
        code, events = self._history_command("--known-output", str(empty))
        self.assertEqual(0, code)
        self.assertEqual([], events[-1]["details"]["projects"])
        self.assertFalse((empty / ".booktext_job.json").exists())
        self.assertTrue(events[-1]["details"]["warnings"])

    def test_cloud_placeholder_is_controlled_without_artifact_probe_or_job_write(self):
        placeholder = Path(self.temporary.name) / "icloud_book"
        placeholder.mkdir()
        with mock.patch.object(protocol, "_icloud_managed_path", return_value=True), \
             mock.patch.object(protocol, "artifact_map") as artifacts:
            detail = protocol.inspect_output(placeholder)
        self.assertEqual("PROJECT_FILES_UNAVAILABLE", detail["project"]["availability"])
        self.assertIn("iCloud-managed", detail["project"]["detail"])
        artifacts.assert_not_called()
        self.assertFalse((placeholder / ".booktext_job.json").exists())

    def test_interrupted_ocr_and_translation_are_reopenable_without_repeating_checkpoints(self):
        protocol.update_job(self.root, status="cancelled", last_phase="ocr", partial=True)
        chunks = self.root / "chunks"; chunks.mkdir()
        plan = {"chunks": [{"chunk_id": "chunk-0001", "status": "complete"}, {"chunk_id": "chunk-0002", "status": "pending"}]}
        (chunks / "chunk_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        project_history.record(self.root, protocol.ensure_job(self.root), history_path=self.history)
        code, events = self._history_command()
        self.assertEqual(0, code)
        project = events[-1]["details"]["projects"][0]
        self.assertEqual("INTERRUPTED_RESUMABLE", project["state"])
        self.assertEqual(plan, json.loads((chunks / "chunk_plan.json").read_text(encoding="utf-8")))
        protocol.update_job(self.root, status="completed", last_phase="translation", partial=True)
        translation_chunks = self.root / "translations" / "chunks"; translation_chunks.mkdir(parents=True)
        translation_plan = {"chunks": [{"chunk_id": "chunk-0001", "status": "complete"}, {"chunk_id": "chunk-0002", "status": "running"}]}
        (translation_chunks / "translation_plan.json").write_text(json.dumps(translation_plan), encoding="utf-8")
        code, events = self._history_command()
        project = events[-1]["details"]["projects"][0]
        self.assertEqual("INTERRUPTED_RESUMABLE", project["state"])
        self.assertEqual("RESUMABLE", project["translation_state"])
        self.assertEqual(translation_plan, json.loads((translation_chunks / "translation_plan.json").read_text(encoding="utf-8")))

    def test_book_and_translation_ready_states_come_from_existing_artifacts(self):
        (self.root / "review_priority.json").write_text(json.dumps({"counts": {"HIGH": 0, "MEDIUM": 0}, "items": []}), encoding="utf-8")
        (self.root / "final_build.json").write_text("{}", encoding="utf-8")
        (self.root / "book_document.json").write_text("{}", encoding="utf-8")
        project_history.record(self.root, protocol.ensure_job(self.root), history_path=self.history)
        code, events = self._history_command(); self.assertEqual("BOOK_READY", events[-1]["details"]["projects"][0]["state"])
        translations = self.root / "translations"; translations.mkdir()
        (translations / "en-ru.json").write_text(json.dumps({"artifact_type": "booktext.translation", "validation": {"valid": True, "stale": False}}), encoding="utf-8")
        code, events = self._history_command(); self.assertEqual("TRANSLATION_READY", events[-1]["details"]["projects"][0]["state"])

    def test_pipeline_projection_is_read_from_project_owned_state(self):
        from tools import sequential_pipeline
        definition = sequential_pipeline.default_definition(review_policy="skip_human_review", export_formats=["docx"])
        pipeline, _ = sequential_pipeline.create(self.root, source=self.source, definition=definition)
        sequential_pipeline.mark_running(self.root, pipeline, pipeline["stages"][0])
        protocol.update_job(self.root, status="running", last_phase="ocr")
        project_history.record(self.root, protocol.read_job(self.root), history_path=self.history)
        code, events = self._history_command()
        self.assertEqual(0, code)
        row = events[-1]["details"]["projects"][0]
        self.assertEqual("PIPELINE_RUNNING", row["state"])
        self.assertEqual("process", row["pipeline_current_stage"])
        self.assertEqual([], row["pipeline_completed_stages"])

    def test_missing_source_workspace_and_malformed_entry_are_controlled(self):
        project_history.record(self.root, self.state, history_path=self.history)
        self.source.unlink()
        code, events = self._history_command()
        project = events[-1]["details"]["projects"][0]
        self.assertEqual("SOURCE_UNAVAILABLE", project["availability"])
        self.assertEqual("REVIEW_REQUIRED", project["state"])
        self.assertTrue(project["reopen_available"])
        self.history.write_text(json.dumps({"schema_version": "1.0", "artifact_type": "booktext.project_history", "entries": [{"bad": True}, {"project_id": "gone", "output_dir": str(self.root / "gone")}]}), encoding="utf-8")
        code, events = self._history_command(); details = events[-1]["details"]
        self.assertEqual("PROJECT_FILES_UNAVAILABLE", details["projects"][0]["availability"])
        self.assertTrue(details["warnings"])

    def test_remove_from_history_never_deletes_project_data(self):
        entry = project_history.record(self.root, self.state, history_path=self.history)
        code, events = machine(lambda: protocol.main(["project-remove", entry["project_id"], "--history-path", str(self.history)]))
        self.assertEqual(0, code)
        self.assertTrue(events[-1]["details"]["removed"])
        self.assertFalse(events[-1]["details"]["project_data_deleted"])
        self.assertTrue((self.root / "book.json").exists())

    def test_default_history_never_records_fragile_temp_workspace(self):
        with mock.patch.object(project_history, "default_path", return_value=self.history):
            self.assertIsNone(project_history.record(self.root, self.state))
            recovered = project_history.reconcile(known_outputs=[], inspect=protocol.inspect_output)
        self.assertEqual([], recovered["projects"])


if __name__ == "__main__":
    unittest.main()
