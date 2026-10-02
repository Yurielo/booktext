import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import booktext


class SuryaTextConversionTests(unittest.TestCase):
    def test_preserves_letters_and_block_boundaries(self):
        text = booktext._surya_html_to_text(
            "<p>The treatment &amp; Stomach.</p><p>Next text.</p>"
        )
        self.assertEqual(text, "The treatment & Stomach.\nNext text.")

    def test_separates_table_cells_without_losing_digits(self):
        text = booktext._surya_html_to_text(
            "<table><tr><td>352.05</td><td>353.50</td></tr></table>"
        )
        self.assertEqual(text, "352.05 353.50")


class PageSelectionTests(unittest.TestCase):
    def test_accepts_one_page_a_list_and_mixed_ranges(self):
        self.assertEqual(booktext.choose_pages(20, "5", None), [5])
        self.assertEqual(booktext.choose_pages(20, "5,7,11", None), [5, 7, 11])
        self.assertEqual(booktext.choose_pages(20, "5-7,11,15-16", None), [5, 6, 7, 11, 15, 16])

    def test_processes_every_page_except_explicit_list_or_range(self):
        self.assertEqual(booktext.choose_pages(8, None, "5"), [1, 2, 3, 4, 6, 7, 8])
        self.assertEqual(booktext.choose_pages(8, None, "2,5,7"), [1, 3, 4, 6, 8])
        self.assertEqual(booktext.choose_pages(8, None, "2-4,7"), [1, 5, 6, 8])

    def test_can_exclude_from_an_explicit_subset(self):
        self.assertEqual(booktext.choose_pages(20, "5-11", "7,9-10"), [5, 6, 8, 11])


class RegionExclusionTests(unittest.TestCase):
    def test_rejects_automatic_handwriting_region_that_covers_most_of_page(self):
        response = json.dumps({"regions": [{
            "type": "handwriting", "bbox": [5, 5, 95, 95], "confidence": 0.99,
        }]})
        with patch.object(booktext, "ollama_request", return_value=response):
            regions = booktext.detect_visual_regions(Path("page.png"), "model")
        self.assertEqual(regions, [])

    def test_manual_region_map_is_validated_and_marks_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "regions.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "pages": [{
                    "page": 5,
                    "regions": [{
                        "type": "handwriting",
                        "action": "exclude",
                        "bbox_percent": [10, 20, 30, 40],
                    }],
                }],
            }), encoding="utf-8")
            result = booktext.load_region_map(path)

        self.assertEqual(result[5][0]["ocr_action"], "exclude")
        self.assertEqual(result[5][0]["source"], "manual_region_map")

    def test_searchable_pdf_is_not_created_for_excluded_regions(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(booktext, "run") as run:
            created = booktext.create_searchable_pdf(
                [{"visuals": [{"ocr_action": "exclude"}]}],
                Path(temp) / "result.pdf", Path(temp), "eng",
            )
        self.assertFalse(created)
        run.assert_not_called()


class VisionQATests(unittest.TestCase):
    def _compare(self, surya, vision):
        payload = {
            "words": [
                {
                    "text": vision,
                    "confidence": 0.9,
                    "bbox": {"x": 0, "y": 0, "width": 1, "height": 1},
                }
            ]
        }
        process = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with tempfile.TemporaryDirectory() as temp, patch.object(
            booktext, "require_vision"
        ), patch.object(booktext, "run", return_value=process):
            return booktext.vision_qa_page(
                Path(temp) / "page.png", 1, surya, Path(temp) / "vision"
            )

    def test_preserves_numeric_punctuation_and_signs_for_review(self):
        for surya, vision in (("1.5", "15"), ("-5", "5"), ("352.05", "352.5")):
            with self.subTest(surya=surya, vision=vision):
                self.assertTrue(self._compare(surya, vision)["differences"])

    def test_records_vision_bottom_left_coordinate_provenance(self):
        result = self._compare("alpha", "beta")
        self.assertEqual(
            result["differences"][0]["vision_bbox_coordinate_space"],
            "vision_normalized_bottom_left",
        )

    def test_ignores_only_alphabetic_word_segmentation(self):
        self.assertFalse(self._compare("treatment", "treat ment")["differences"])
        self.assertTrue(self._compare("1.5", "1 5")["differences"])


class ReviewOutputTests(unittest.TestCase):
    def test_failed_qa_is_reported_as_partial_not_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            json_path = root / "review.json"
            text_path = root / "review.txt"
            booktext.write_review_outputs(
                json_path,
                text_path,
                [{"page": 3, "error": "Vision OCR failed"}],
                [],
            )
            report = json.loads(json_path.read_text(encoding="utf-8"))
            text = text_path.read_text(encoding="utf-8")

        self.assertEqual(report["status"], "partial")
        self.assertIn("НЕПОЛНАЯ ПРОВЕРКА", text)
        self.assertNotIn("Значимых расхождений", text)


class SuryaOutputSafetyTests(unittest.TestCase):
    def test_context_reserves_image_prompt_without_changing_generation(self):
        with patch.dict(os.environ, {}, clear=True):
            env = booktext.surya_runtime_environment()
        self.assertEqual(env["SURYA_MAX_TOKENS_FULL_PAGE"], "12288")
        self.assertEqual(env["SURYA_INFERENCE_CTX_PER_SLOT"], "20480")
        self.assertEqual(env["SURYA_INFERENCE_PARALLEL"], "2")
        with patch.dict(os.environ, {"SURYA_MAX_TOKENS_FULL_PAGE": "20000",
                                     "SURYA_INFERENCE_CTX_PER_SLOT": "30000"}, clear=True):
            self.assertEqual(booktext.surya_runtime_environment()["SURYA_INFERENCE_CTX_PER_SLOT"], "30000")
        with patch.dict(os.environ, {"SURYA_INFERENCE_CTX_SIZE": "12288"}, clear=True):
            with self.assertRaisesRegex(ValueError, "cannot fit"):
                booktext.surya_runtime_environment()

    def test_figure_only_page_is_a_valid_empty_ocr_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / "page-0001.png", root / "page-0002.png"
            first.write_bytes(b"figure")
            second.write_bytes(b"text")
            output, work = root / "surya", root / "work"

            def write_surya_result(command, **kwargs):
                staging = Path(command[command.index("--output_dir") + 1])
                result_dir = staging / "surya-input"
                result_dir.mkdir(parents=True)
                (result_dir / "results.json").write_text(json.dumps({
                    "page-0001": [{}],
                    "page-0002": [{"text": "Recovered text"}],
                }), encoding="utf-8")

            with patch.object(booktext, "require_surya"), patch.object(
                booktext, "run", side_effect=write_surya_result
            ):
                texts = booktext._recognize_pages_surya_batch(
                    [(1, first), (2, second)], output, work
                )

            self.assertEqual(texts, {1: "", 2: "Recovered text"})
            self.assertTrue((output / "surya-input" / "results.json").is_file())

    def test_missing_surya_page_record_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / "page-0001.png", root / "page-0002.png"
            first.write_bytes(b"one")
            second.write_bytes(b"two")

            def write_incomplete_result(command, **kwargs):
                staging = Path(command[command.index("--output_dir") + 1])
                result_dir = staging / "surya-input"
                result_dir.mkdir(parents=True)
                (result_dir / "results.json").write_text(
                    json.dumps({"page-0001": [{"text": "one"}]}), encoding="utf-8"
                )

            with patch.object(booktext, "require_surya"), patch.object(
                booktext, "run", side_effect=write_incomplete_result
            ), self.assertRaisesRegex(RuntimeError, "страниц: 2"):
                booktext._recognize_pages_surya_batch(
                    [(1, first), (2, second)], root / "surya", root / "work"
                )

    def test_failed_rerun_keeps_previous_raw_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = root / "page.png"
            page.write_bytes(b"page")
            output = root / "surya"
            output.mkdir()
            (output / "previous.txt").write_text("keep", encoding="utf-8")

            with patch.object(booktext, "require_surya"), patch.object(
                booktext, "run", side_effect=subprocess.CalledProcessError(1, ["surya"])
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    booktext.recognize_pages_surya([(1, page)], output, root / "work")

            self.assertEqual(
                (output / "previous.txt").read_text(encoding="utf-8"), "keep"
            )


if __name__ == "__main__":
    unittest.main()
