import json
import tempfile
import unittest
from pathlib import Path

from tools import chunk_lifecycle


class ChunkLifecycleTests(unittest.TestCase):
    def make_plan(self, root, pages, **kwargs):
        return chunk_lifecycle.create_or_load_plan(
            root, job_id="job-test", source_fp="source-a", operation="surya_ocr",
            page_numbers=pages, options={"engine": "surya"}, **kwargs,
        )

    def test_small_and_large_auto_plans_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            one = self.make_plan(root / "one", [1])
            seven = self.make_plan(root / "seven", list(range(1, 8)))
            large = self.make_plan(root / "large", list(range(1, 27)))
            again = self.make_plan(root / "large", list(range(1, 27)))
            self.assertEqual([item["owned_pages"] for item in one["chunks"]], [[1]])
            self.assertEqual([item["owned_pages"] for item in seven["chunks"]], [list(range(1, 8))])
            self.assertEqual([item["owned_range"] for item in large["chunks"]], [[1, 8], [9, 16], [17, 24], [25, 26]])
            self.assertEqual(large, again)
            self.assertEqual(large["chunks"][1]["context_before_pages"], [8])
            self.assertEqual(large["chunks"][1]["context_after_pages"], [17])

    def test_explicit_size_and_source_change_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = self.make_plan(root, list(range(1, 12)), chunk_size=5)
            self.assertEqual([item["owned_range"] for item in plan["chunks"]], [[1, 5], [6, 10], [11, 11]])
            with self.assertRaises(chunk_lifecycle.ChunkPlanError):
                chunk_lifecycle.create_or_load_plan(root, job_id="job-test", source_fp="source-b", operation="surya_ocr", page_numbers=list(range(1, 12)), options={"engine": "surya"}, chunk_size=5)
            self.assertEqual(json.loads(chunk_lifecycle.plan_path(root).read_text())["status"], "stale")

    def test_checkpoint_resume_and_corrupt_chunk_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); plan = self.make_plan(root, list(range(1, 13)), chunk_size=5)
            first, second = plan["chunks"][:2]
            for chunk in (first, second):
                chunk_lifecycle.begin_attempt(root, plan, chunk)
                payload = {f"page-{page:04d}": [{"text": str(page)}] for page in chunk["owned_pages"]}
                chunk_lifecycle.commit_checkpoint(root, plan, chunk, payload, duration_seconds=.1)
            self.assertIsNotNone(chunk_lifecycle.valid_checkpoint(root, first))
            self.assertIsNotNone(chunk_lifecycle.valid_checkpoint(root, second))
            result = root / second["artifacts"]["result"]
            result.write_text("{}", encoding="utf-8")
            self.assertIsNotNone(chunk_lifecycle.valid_checkpoint(root, first))
            self.assertIsNone(chunk_lifecycle.valid_checkpoint(root, second))

    def test_compaction_requires_verified_assembly_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); plan = self.make_plan(root, [1, 2], chunk_size=2); chunk = plan["chunks"][0]
            chunk_lifecycle.begin_attempt(root, plan, chunk)
            chunk_lifecycle.commit_checkpoint(root, plan, chunk, {"page-0001": [{"text": "one"}], "page-0002": [{"text": "two"}]}, duration_seconds=.1)
            blocked = chunk_lifecycle.compact(root)
            self.assertFalse(blocked["cleanup_allowed"])
            self.assertTrue((root / chunk["artifacts"]["result"]).exists())
            chunk_lifecycle.mark_assembly(root, plan, complete=True, validation={"pages": "PASS"})
            cleaned = chunk_lifecycle.compact(root)
            self.assertTrue(cleaned["cleanup_allowed"])
            self.assertFalse((root / chunk["artifacts"]["result"]).exists())
            canonical = {"page-0001": [{"text": "one"}], "page-0002": [{"text": "two"}]}
            compacted = json.loads(chunk_lifecycle.plan_path(root).read_text())
            self.assertEqual(chunk_lifecycle.valid_compacted_checkpoint(canonical, compacted["chunks"][0]), canonical)
            self.assertEqual(chunk_lifecycle.compact(root)["files_deleted"], 0)

    def test_cleanup_refuses_unmarked_or_escaping_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(chunk_lifecycle.ChunkPlanError):
                chunk_lifecycle.compact(root)


if __name__ == "__main__":
    unittest.main()
