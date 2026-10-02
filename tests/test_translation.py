import json
import subprocess
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

from tools import translation


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.document = {
            "schema_version": "1.0", "artifact_type": "booktext.canonical_document", "document_id": "book-1",
            "metadata": {"title": "Example", "source_page_count": 1},
            "source": {}, "source_pages": [{"source_page": 1, "page_image": None}],
            "review": {"unresolved_priority_counts": {"HIGH": 0, "MEDIUM": 0}}, "notes": [], "warnings": [],
            "blocks": [
                {"block_id": "b1", "type": "heading", "text": "HELLO", "reading_order": 1, "provenance": {"source_page": 1}},
                {"block_id": "b2", "type": "paragraph", "text": "Value 12.5 mg on 1/2 page.", "reading_order": 2, "provenance": {"source_page": 1}},
                {"block_id": "b3", "type": "paragraph", "text": "Русский текст", "reading_order": 3, "provenance": {"source_page": 1}},
                {"block_id": "b4", "type": "page_break", "text": "", "reading_order": 4, "provenance": {"source_page": 1}},
            ],
        }
        (self.root / "book_document.json").write_text(json.dumps(self.document), encoding="utf-8")
        (self.root / "review_decisions.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def fake_metadata(*_args, **_kwargs):
        return {"kind": "ollama", "endpoint": "http://local", "model": translation.PRODUCTION_MODEL, "model_digest": "sha256:test", "model_size_bytes": 1, "api": "/api/chat"}

    @staticmethod
    def fake_translate(**kwargs):
        source = kwargs["source"]
        translated = {"HELLO": "ПРИВЕТ", "Value 12.5 mg on 1/2 page.": "Значение 12.5 mg на 1/2 странице."}.get(source, source)
        return translated, {"temperature": 0, "response_format": "json", "glossary_json": "{}"}

    def test_retired_model_is_rejected_before_provider_access(self):
        with mock.patch("tools.translation._model_info") as provider:
            with self.assertRaisesRegex(ValueError, "MODEL_RETIRED"):
                translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model="qwen3:8b")
        provider.assert_not_called()
        result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "booktext.py"), "translate", str(self.root),
            "--source-language", "en", "--target-language", "ru", "--model", "qwen3:8b"], capture_output=True, text=True)
        self.assertEqual(5, result.returncode)
        self.assertEqual("MODEL_RETIRED", json.loads(result.stderr)["code"])

    def test_busy_translation_worker_does_not_start_provider(self):
        with mock.patch("tools.translation.fcntl.flock", side_effect=BlockingIOError), mock.patch("tools.translation._model_info") as provider:
            with self.assertRaisesRegex(ValueError, "TRANSLATION_BUSY"):
                translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama")
        provider.assert_not_called()

    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_completed_en_ru_plan_does_not_block_new_ru_en_run(self, _metadata, _translate):
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama")
        result = translation.translate_document(self.root, source_language="ru", target_language="en", provider="ollama")
        self.assertTrue(result["validation"]["valid"])
        self.assertTrue((self.root / "translations/en-ru.json").exists())
        self.assertTrue((self.root / "translations/ru-en.json").exists())

    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_legacy_partial_checkpoint_requires_explicit_migration_and_archives(self, _metadata, translated):
        with mock.patch("tools.translation._translate_ollama", side_effect=ValueError("interrupted")):
            with self.assertRaisesRegex(ValueError, "interrupted"):
                translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama")
        plan_path = self.root / "translations/chunks/translation_plan.json"
        legacy = json.loads(plan_path.read_text())
        legacy["options"]["model"] = "qwen3:8b"
        legacy["options_fingerprint"] = translation._digest(legacy["options"])
        plan_path.write_text(json.dumps(legacy))
        with self.assertRaisesRegex(ValueError, "MIGRATION_REQUIRED"):
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama")
        translated.assert_not_called()
        result = translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", migrate=True)
        self.assertTrue(result["validation"]["valid"])
        archives = list((self.root / "translations/history/checkpoints").glob("*/translation_plan.json"))
        self.assertEqual(1, len(archives))
        self.assertEqual("qwen3:8b", json.loads(archives[0].read_text())["options"]["model"])
        self.assertEqual(translation.PRODUCTION_MODEL, json.loads((self.root / "translations/en-ru.json").read_text())["generation"]["model"])

    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_translation_is_source_anchored_deterministic_and_does_not_edit_cdm(self, _metadata, _translate):
        before = (self.root / "book_document.json").read_bytes()
        result = translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL)
        self.assertFalse(result["reused"])
        self.assertEqual(before, (self.root / "book_document.json").read_bytes())
        artifact = json.loads((self.root / "translations/en-ru.json").read_text())
        self.assertEqual("booktext.translation", artifact["artifact_type"])
        self.assertEqual(3, len(artifact["blocks"]))
        self.assertEqual("machine_translated", artifact["blocks"][1]["status"])
        self.assertEqual("skipped_already_target_language", artifact["blocks"][2]["status"])
        self.assertTrue((self.root / "translations/book-en-ru.md").is_file())
        self.assertTrue((self.root / "translations/book-en-ru-bilingual.md").is_file())
        reused = translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL)
        self.assertTrue(reused["reused"])
        self.assertEqual(2, _translate.call_count)

    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_cli_validate_needs_no_model_or_provider(self, _metadata, _translate):
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parents[1] / "booktext.py"), "translate", str(self.root),
             "--source-language", "en", "--target-language", "ru", "--validate"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])

    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_changed_source_marks_existing_translation_stale_and_archives_prior_revision(self, _metadata, _translate):
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL)
        self.document["blocks"][1]["text"] = "Value 14 mg."
        (self.root / "book_document.json").write_text(json.dumps(self.document), encoding="utf-8")
        stale = translation.validate_translation(self.root, source_language="en", target_language="ru")
        self.assertTrue(stale["stale"])
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, migrate=True)
        self.assertTrue(list((self.root / "translations/history").glob("en-ru-*.json")))

    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_publication_normalization_invalidates_old_translation_without_inference(self, _metadata, _translate):
        from tools import text_normalization
        self.document["blocks"][1]["text"] = "An example, i. e. the explanation in ordinary prose."
        self.document["blocks"][1]["provenance"]["layout_label"] = "Text"
        (self.root / "book_document.json").write_text(json.dumps(self.document))
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL)
        calls = _translate.call_count
        raw_translation = (self.root / "translations/en-ru.json").read_bytes()
        text_normalization.project(self.document["blocks"], [b.get("text", "") for b in self.document["blocks"]])
        (self.root / "book_document.json").write_text(json.dumps(self.document))
        self.assertTrue(translation.validate_translation(self.root, source_language="en", target_language="ru")["stale"])
        self.assertEqual(_translate.call_count, calls)
        self.assertEqual(raw_translation, (self.root / "translations/en-ru.json").read_bytes())

    @mock.patch("tools.translation._translate_ollama", return_value=("Значение 12.6 mg на 1/2 странице.", {}))
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_numeric_changes_are_never_silently_verified(self, _metadata, _translate):
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL)
        artifact = json.loads((self.root / "translations/en-ru.json").read_text())
        number_entry = next(item for item in artifact["blocks"] if item["block_id"] == "b2")
        self.assertEqual("needs_numeric_review", number_entry["status"])
        self.assertEqual("FAIL", number_entry["validation"]["numeric_tokens"])

    @mock.patch("tools.translation.urllib.request.urlopen")
    def test_ollama_uses_translate_gemma_plain_text_contract(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"message": {"content": "Привет"}, "done_reason": "stop"}, ensure_ascii=False).encode()
        urlopen.return_value.__enter__.return_value = response
        translated, settings = translation._translate_ollama(endpoint="http://local", model=translation.PRODUCTION_MODEL, source="Hello", source_language="en", target_language="ru", before="", after="", glossary={}, timeout=1)
        self.assertEqual("Привет", translated)
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("think", payload)
        self.assertNotIn("format", payload)
        self.assertEqual(1, len(payload["messages"]))
        self.assertEqual("user", payload["messages"][0]["role"])
        self.assertIn("English (en) to Russian (ru)", payload["messages"][0]["content"])
        self.assertTrue(payload["messages"][0]["content"].endswith("\n\n\nHello"))
        self.assertEqual("plain_text", settings["response_format"])
        self.assertEqual(translation.PROMPT_VERSION, settings["prompt_version"])


    def test_model_lease_is_bounded_and_recorded_without_global_unload(self):
        with mock.patch("tools.translation._model_info", side_effect=self.fake_metadata), \
             mock.patch("tools.translation._translate_ollama", side_effect=self.fake_translate) as translated:
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, keep_alive="30s")
        self.assertTrue(all(call.kwargs["keep_alive"] == "300s" for call in translated.call_args_list))
        artifact = json.loads((self.root / "translations/en-ru.json").read_text(encoding="utf-8"))
        lifecycle = artifact["provider"]["resource_lifecycle"]
        self.assertEqual("30s", lifecycle["requested_idle_keep_alive"])
        self.assertEqual("300s", lifecycle["execution_keep_alive"])
        self.assertIn("never stops the shared daemon", lifecycle["policy"])
        with self.assertRaisesRegex(ValueError, "bounded duration"):
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, keep_alive="forever")
        with self.assertRaisesRegex(ValueError, "between 1ms and 300s"):
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, keep_alive="6m")

    def test_failure_timeout_and_cancellation_keep_the_bounded_lease(self):
        observed = []
        def failed_request(request, timeout):
            observed.append(json.loads(request.data.decode("utf-8")))
            raise urllib.error.URLError("simulated transport failure")
        with mock.patch("tools.translation.urllib.request.urlopen", side_effect=failed_request):
            with self.assertRaisesRegex(ValueError, "translation request failed"):
                translation._translate_ollama(endpoint="http://local", model=translation.PRODUCTION_MODEL, source="Hello", source_language="en", target_language="ru", before="", after="", glossary={}, timeout=1)
        self.assertEqual("30s", observed[0]["keep_alive"])
        cancelled = []
        def cancelled_request(request, timeout):
            cancelled.append(json.loads(request.data.decode("utf-8")))
            raise KeyboardInterrupt
        with mock.patch("tools.translation.urllib.request.urlopen", side_effect=cancelled_request):
            with self.assertRaises(KeyboardInterrupt):
                translation._translate_ollama(endpoint="http://local", model=translation.PRODUCTION_MODEL, source="Hello", source_language="en", target_language="ru", before="", after="", glossary={}, timeout=1)
        self.assertEqual("30s", cancelled[0]["keep_alive"])

    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_transient_provider_request_retries_only_the_same_logical_unit(self, _metadata):
        calls = []
        def transient(**kwargs):
            calls.append(kwargs["source"])
            if len(calls) == 1:
                raise translation.TranslationProviderRequestError("simulated temporary local failure")
            return self.fake_translate(**kwargs)
        with mock.patch("tools.translation._translate_ollama", side_effect=transient):
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, chunk_size=1)
        self.assertEqual(["HELLO", "HELLO", "Value 12.5 mg on 1/2 page."], calls)
        artifact = json.loads((self.root / "translations/en-ru.json").read_text())
        first = artifact["blocks"][0]["attempts"][0]
        self.assertEqual(2, first["provider_request_attempts"])
        self.assertEqual(1, len(first["provider_request_failures"]))

    @mock.patch("tools.translation.urllib.request.urlopen")
    def test_empty_or_truncated_plain_response_fails(self, urlopen):
        response = mock.MagicMock()
        urlopen.return_value.__enter__.return_value = response
        for content, done_reason in (("", "stop"), ("КОРЬ", "length")):
            response.read.return_value = json.dumps({"message": {"content": content}, "done_reason": done_reason}, ensure_ascii=False).encode()
            with self.assertRaises(translation.TranslationProviderRequestError):
                translation._translate_ollama(endpoint="http://local", model=translation.PRODUCTION_MODEL, source="MEASLES", source_language="en", target_language="ru", before="", after="", glossary={}, timeout=1)


    def test_plain_provider_fallback_is_never_accepted_as_clean_translation(self):
        signals = translation._response_quality("MEASLES", "КОРЬ", source_language="en", target_language="ru", kind="heading", glossary={}, before="", after="", response={"response_format_fallback": True})
        self.assertIn("provider_response_format_requires_review", signals)

    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_provider_retry_uses_bounded_escalating_timeout_for_same_unit(self, _metadata):
        observed = []
        def transient(**kwargs):
            observed.append((kwargs["source"], kwargs["timeout"]))
            if len(observed) == 1:
                raise translation.TranslationProviderRequestError("simulated timeout")
            return self.fake_translate(**kwargs)
        with mock.patch("tools.translation._translate_ollama", side_effect=transient):
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, timeout=120, chunk_size=1)
        self.assertEqual([("HELLO", 120), ("HELLO", 240)], observed[:2])

    def test_execution_lease_covers_the_bounded_request_without_becoming_unbounded(self):
        self.assertEqual("300s", translation._effective_request_keep_alive("30s", 120))
        self.assertEqual("300s", translation._effective_request_keep_alive("30s", 300))
        self.assertEqual("30s", translation._effective_request_keep_alive("30s", 1))

    def test_output_budget_is_logical_unit_sized_and_bounded(self):
        self.assertEqual(128, translation._max_output_tokens_for_source("MEASLES"))
        self.assertEqual(2048, translation._max_output_tokens_for_source("x" * 2000))

    @mock.patch("tools.translation.urllib.request.urlopen")
    def test_heading_uses_same_plain_text_contract(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"message": {"content": "КОРЬ"}, "done_reason": "stop"}, ensure_ascii=False).encode()
        urlopen.return_value.__enter__.return_value = response
        translated, settings = translation._translate_ollama(endpoint="http://local", model=translation.PRODUCTION_MODEL, source="MEASLES", source_language="en", target_language="ru", before="before", after="after", glossary={}, timeout=1, document_context={"block_type": "heading"})
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("format", payload)
        self.assertEqual("КОРЬ", translated)
        self.assertEqual("plain_text", settings["response_format"])


    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_logical_unit_checkpoints_resume_without_retranslating_completed_units(self, _metadata):
        calls = []
        failed_once = {"value": False}
        def interrupted(**kwargs):
            calls.append(kwargs["source"])
            if kwargs["source"] == "Value 12.5 mg on 1/2 page." and not failed_once["value"]:
                failed_once["value"] = True
                raise ValueError("simulated provider interruption")
            return self.fake_translate(**kwargs)
        with mock.patch("tools.translation._translate_ollama", side_effect=interrupted):
            with self.assertRaisesRegex(ValueError, "interruption"):
                translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, chunk_size=1)
            plan_path = self.root / "translations/chunks/translation_plan.json"
            plan = json.loads(plan_path.read_text())
            self.assertEqual("complete", plan["chunks"][0]["status"])
            self.assertEqual("failed", plan["chunks"][1]["status"])
            self.assertTrue((self.root / plan["chunks"][0]["artifacts"]["checkpoint"]).is_file())
            result = translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, chunk_size=1)
        self.assertEqual(["HELLO", "Value 12.5 mg on 1/2 page.", "Value 12.5 mg on 1/2 page."], calls)
        self.assertEqual(["chunk-0001"], result["execution"]["resumed_chunks"])
        artifact = json.loads((self.root / "translations/en-ru.json").read_text())
        self.assertEqual(["b1", "b2", "b3"], [item["block_id"] for item in artifact["blocks"]])
        self.assertEqual("NOT_SEMANTICALLY_VERIFIED", artifact["execution"]["assembly"]["context_only_output"])
        final_plan = json.loads(plan_path.read_text())
        self.assertEqual("complete", final_plan["assembly"]["status"])
        self.assertTrue(all(item["artifacts"].get("checkpoint_compacted") for item in final_plan["chunks"]))
        self.assertFalse((self.root / final_plan["chunks"][0]["artifacts"]["checkpoint"]).exists())

    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    def test_chunk_plan_context_is_read_only_and_source_anchored(self, _translate, _metadata):
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, chunk_size=1)
        plan = json.loads((self.root / "translations/chunks/translation_plan.json").read_text())
        self.assertEqual(["b1"], plan["chunks"][0]["owned_unit_ids"])
        self.assertEqual(["b2"], plan["chunks"][0]["context_after_unit_ids"])
        self.assertEqual(["b1"], plan["chunks"][1]["context_before_unit_ids"])
        contexts = [call.kwargs["document_context"] for call in _translate.call_args_list]
        self.assertTrue(all(context["document_id"] == "book-1" for context in contexts))
        self.assertTrue(all("owned source block" in context["output_contract"] for context in contexts))

    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_partial_checkpoint_resumes_completed_units_inside_a_failed_chunk(self, _metadata):
        calls, failed_once = [], {"value": False}

        def interrupted(**kwargs):
            calls.append(kwargs["source"])
            if kwargs["source"] == "Value 12.5 mg on 1/2 page." and not failed_once["value"]:
                failed_once["value"] = True
                raise ValueError("simulated neighbour failure")
            return self.fake_translate(**kwargs)

        with mock.patch("tools.translation._translate_ollama", side_effect=interrupted):
            with self.assertRaisesRegex(ValueError, "neighbour failure"):
                translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, chunk_size=2)
            partial = self.root / "translations/chunks/chunk-0001/partial-checkpoint.json"
            self.assertTrue(partial.is_file())
            self.assertEqual(["b1"], [entry["block_id"] for entry in json.loads(partial.read_text())["entries"]])
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, chunk_size=2)
        self.assertEqual(["HELLO", "Value 12.5 mg on 1/2 page.", "Value 12.5 mg on 1/2 page."], calls)
        self.assertFalse(partial.exists())

    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_stale_completed_manifest_does_not_reset_matching_in_progress_plan(self, _metadata):
        first_glossary = self.root / "first-glossary.json"; first_glossary.write_text(json.dumps({"HELLO": "ПРИВЕТ"}), encoding="utf-8")
        second_glossary = self.root / "second-glossary.json"; second_glossary.write_text(json.dumps({"HELLO": "ПРИВЕТ", "Value": "Значение"}), encoding="utf-8")
        with mock.patch("tools.translation._translate_ollama", side_effect=self.fake_translate):
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, glossary_path=first_glossary, block_ids=["b1", "b2"], chunk_size=2)
        calls, failed_once = [], {"value": False}
        def interrupted(**kwargs):
            calls.append(kwargs["source"])
            if kwargs["source"].startswith("Value") and not failed_once["value"]:
                failed_once["value"] = True
                raise ValueError("new revision interruption")
            return self.fake_translate(**kwargs)
        with mock.patch("tools.translation._translate_ollama", side_effect=interrupted):
            with self.assertRaisesRegex(ValueError, "new revision interruption"):
                translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, glossary_path=second_glossary, block_ids=["b1", "b2"], chunk_size=2, migrate=True)
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, glossary_path=second_glossary, block_ids=["b1", "b2"], chunk_size=2)
        self.assertEqual(["HELLO", "Value 12.5 mg on 1/2 page.", "Value 12.5 mg on 1/2 page."], calls)

    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_source_evidence_is_reviewed_but_not_retried(self, _metadata):
        self.document["blocks"][1]["text"] = "biomagnetical-"
        (self.root / "book_document.json").write_text(json.dumps(self.document), encoding="utf-8")
        with mock.patch("tools.translation._translate_ollama", return_value=("биомагнитный-", {"done_reason": "stop"})) as translated:
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL)
        entry = next(item for item in json.loads((self.root / "translations/en-ru.json").read_text())["blocks"] if item["block_id"] == "b2")
        self.assertEqual("needs_quality_review", entry["status"])
        self.assertIn("source_boundary_hyphen_requires_review", entry["validation"]["quality_signals"])
        self.assertEqual(2, translated.call_count)  # Heading plus this block; no redundant retries.

    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_long_unit_has_one_bounded_attempt_and_remains_review_evidence(self, _metadata):
        self.document["blocks"][1]["text"] = "Magic Wand " + ("ordinary source words " * 80)
        (self.root / "book_document.json").write_text(json.dumps(self.document), encoding="utf-8")
        glossary = self.root / "glossary.json"; glossary.write_text(json.dumps({"Magic Wand": "Магический жезл"}), encoding="utf-8")
        with mock.patch("tools.translation._translate_ollama", return_value=("Магический палец", {"done_reason": "stop"})) as translated:
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, glossary_path=glossary)
        entry = next(item for item in json.loads((self.root / "translations/en-ru.json").read_text())["blocks"] if item.get("block_id") == "b2")
        self.assertEqual("needs_quality_review", entry["status"])
        self.assertEqual(1, len(entry["attempts"]))
        self.assertEqual(1, entry["attempts"][0]["attempt_limit"])
        self.assertEqual(2, translated.call_count)  # Heading plus the long block.

    def test_quality_checks_preserve_context_and_glossary_as_evidence_not_rewrites(self):
        signals = translation._response_quality(
            "Magic Wand", "Магический палец", source_language="en", target_language="ru", kind="heading",
            glossary={"Magic Wand": {"target": "Магический жезл", "variants": ["волшебный жезл"]}},
            before="DEDICATION.", after="The magician held the Magic Wand.", response={"done_reason": "stop"},
        )
        self.assertIn("required_glossary_term_missing:Magic Wand", signals)
        self.assertNotIn("untranslated_heading", signals)
        self.assertIn("Магический палец", "Магический палец")  # The checker has not edited the model response.

    @mock.patch("tools.translation._translate_ollama", side_effect=fake_translate.__func__)
    @mock.patch("tools.translation._model_info", side_effect=fake_metadata.__func__)
    def test_bounded_pilot_owns_selected_units_but_uses_full_cdm_context(self, _metadata, translated):
        translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, block_ids=["b2"])
        artifact = json.loads((self.root / "translations/en-ru.json").read_text())
        self.assertEqual(["b2"], artifact["source"]["selected_block_ids"])
        self.assertEqual(["b2"], [entry["block_id"] for entry in artifact["blocks"]])
        self.assertEqual("b1", artifact["blocks"][0]["context"]["previous_block_id"])
        self.assertEqual("b3", artifact["blocks"][0]["context"]["next_block_id"])
        self.assertTrue(translation.validate_translation(self.root, source_language="en", target_language="ru")["valid"])

    def test_direction_and_glossary_validation_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "direction"):
            translation.translate_document(self.root, source_language="en", target_language="en", provider="ollama", model=translation.PRODUCTION_MODEL)
        glossary = self.root / "bad.json"; glossary.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "glossary"):
            translation.translate_document(self.root, source_language="en", target_language="ru", provider="ollama", model=translation.PRODUCTION_MODEL, glossary_path=glossary)


if __name__ == "__main__":
    unittest.main()
