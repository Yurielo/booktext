import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import booktext


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

    def test_large_job_uses_ordered_automatic_ten_page_batches_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); calls = []
            pages = [fake_page(number) for number in range(1, 24)]
            with patch.object(booktext, "require_surya"), patch.object(booktext, "_recognize_pages_surya_batch", self._runner(calls)):
                texts = booktext.recognize_pages_surya(pages, root / "surya", root / "work")
            self.assertEqual(calls, [list(range(1, 11)), list(range(11, 21)), [21, 22, 23]])
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
            self.assertEqual(calls, [list(range(1, 11)), list(range(1, 6)), list(range(6, 11)), [11, 12]])
            checkpoint = json.loads((root / "surya" / "surya-input" / "batch_checkpoint.json").read_text())
            self.assertEqual(checkpoint["events"][0]["kind"], "automatic_batch_downshift")
            merged = json.loads((root / "surya" / "surya-input" / "results.json").read_text())
            self.assertEqual(sorted(merged), [f"page-{number:04d}" for number in range(1, 13)])


if __name__ == "__main__":
    unittest.main()
