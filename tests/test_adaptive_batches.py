import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import booktext
from tools import chunk_lifecycle


def fake_page(number):
    return number, Path(f"/tmp/page-{number:04d}.png")


class AdaptiveSuryaBatchTests(unittest.TestCase):
    def _runner(self, calls, *, fail_large=False):
        def run_batch(pages, output_dir, _temp_dir):
            calls.append([number for number, _ in pages])
            if fail_large and len(pages) > 5:
                raise RuntimeError("Surya out of memory")
            payload = {
                f"page-{number:04d}": [{"text": f"text {number}"}]
                for number, _ in pages
            }
            path = output_dir / "surya-input" / "results.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            return {number: f"text {number}" for number, _ in pages}
        return run_batch

    def test_large_job_uses_ordered_automatic_eight_page_chunks_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); calls = []
            pages = [fake_page(number) for number in range(1, 24)]
            with patch.object(booktext, "require_surya"), patch.object(booktext, "_recognize_pages_surya_batch", self._runner(calls)):
                texts = booktext.recognize_pages_surya(pages, root / "surya", root / "work")
            self.assertEqual(calls, [list(range(1, 9)), list(range(9, 17)), list(range(17, 24))])
            self.assertEqual(list(texts), list(range(1, 24)))
            checkpoint = json.loads((root / "surya" / "surya-input" / "batch_checkpoint.json").read_text())
            self.assertEqual(checkpoint["completed_pages"], list(range(1, 24)))
            self.assertEqual(checkpoint["status"], "complete")

    def test_resource_failure_downshifts_without_human_input_and_resume_skips_completed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); calls = []
            pages = [fake_page(number) for number in range(1, 13)]
            with patch.object(booktext, "require_surya"), patch.object(booktext, "_recognize_pages_surya_batch", self._runner(calls, fail_large=True)):
                first = booktext.recognize_pages_surya(pages, root / "surya", root / "work")
                second = booktext.recognize_pages_surya(pages, root / "surya", root / "work")
            self.assertEqual(first, second)
            self.assertEqual(calls, [list(range(1, 9)), list(range(1, 5)), list(range(5, 9)), list(range(9, 13))])
            checkpoint = json.loads((root / "surya" / "surya-input" / "batch_checkpoint.json").read_text())
            self.assertEqual(checkpoint["events"][0]["kind"], "automatic_batch_downshift")
            merged = json.loads((root / "surya" / "surya-input" / "results.json").read_text())
            self.assertEqual(sorted(merged), [f"page-{number:04d}" for number in range(1, 13)])

    def test_compacted_completed_plan_resumes_from_canonical_raw_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); calls = []
            pages = [fake_page(number) for number in range(1, 10)]
            with patch.object(booktext, "require_surya"), patch.object(booktext, "_recognize_pages_surya_batch", self._runner(calls)):
                first = booktext.recognize_pages_surya(pages, root / "surya", root / "work")
                chunk_lifecycle.compact(root)
                second = booktext.recognize_pages_surya(pages, root / "surya", root / "work")
            self.assertEqual(first, second)
            self.assertEqual(calls, [list(range(1, 9)), [9]])

    def test_changed_recognition_budget_cannot_reuse_old_page_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp); calls = []; pages = [fake_page(1)]
            with patch.object(booktext, "require_surya"), patch.object(booktext, "_recognize_pages_surya_batch", self._runner(calls)):
                booktext.recognize_pages_surya(pages, root / "surya", root / "work")
                with patch.dict(os.environ, {"SURYA_MAX_TOKENS_FULL_PAGE": "16000"}):
                    with self.assertRaisesRegex(chunk_lifecycle.ChunkPlanError, "stale"):
                        booktext.recognize_pages_surya(pages, root / "surya", root / "work")
            self.assertEqual(calls, [[1]])

    def test_resume_reuses_source_bound_prepared_pages_without_rendering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "source.pdf"; source.write_bytes(b"pdf")
            pages_dir = root / "pages"; pages_dir.mkdir()
            for page in (1, 2): (pages_dir / f"page-{page:04d}.png").write_bytes(b"png")
            with patch.object(booktext, "get_pdf_page_count", return_value=2), patch.object(booktext, "render_pdf_page", side_effect=AssertionError("must not rerender checkpointed pages")):
                pages, total = booktext.prepare_pages([source], pages_dir, root / "work", None, None, reuse_existing=True)
            self.assertEqual(total, 2)
            self.assertEqual([page for page, _path in pages], [1, 2])


if __name__ == "__main__":
    unittest.main()
