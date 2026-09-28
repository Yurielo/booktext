import json
import tempfile
import unittest
from pathlib import Path

from tools.review_clusters import build_clusters
from tools.review_batches import apply_batch, preview, revert_batch
from tools import review_decisions
from tools.review_workspace import ReviewWorkspace


def item(i, page, s="текст", v="тэкст", priority="MEDIUM"):
    return {"item_id":i,"page":page,"priority":priority,"item_type":"ocr_qa","surya":s,"vision":v,"reasons":["text"],"signals":{"qa_kind":"replace","qa_category":"text"}}


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.priority={"schema_version":"1.0","artifact_type":"booktext.review_priority","items":[item("a",1),item("b",2),item("c",3),item("n",4,"1/2","1/3")]}
        self.book={"pages":[{"page":1,"text":"текст"},{"page":2,"text":"текст"},{"page":3,"text":"текст"},{"page":4,"text":"1/2"}]}
        self.pp=self.root/"review_priority.json"; self.bp=self.root/"book.json"; self.dp=self.root/"review_decisions.json"
        self.pp.write_text(json.dumps(self.priority),encoding="utf8");self.bp.write_text(json.dumps(self.book),encoding="utf8")
        self.cluster=next(c for c in build_clusters(self.priority)["clusters"] if c["member_count"]==3)
    def tearDown(self): self.tmp.cleanup()
    def test_strict_eligibility_and_preview_confirmation(self):
        self.assertTrue(self.cluster["batch_eligible"]); self.assertFalse(next(c for c in build_clusters(self.priority)["clusters"] if c["member_item_ids"]==["n"])["batch_eligible"])
        with self.assertRaisesRegex(ValueError,"confirmation"): apply_batch(cluster=self.cluster,priority_path=self.pp,book_path=self.bp,decisions_path=self.dp,action="keep_surya")
        plan=preview(self.cluster,{x["item_id"]:x for x in self.priority["items"]},"keep_surya",["c"])
        self.assertEqual(plan["member_ids"],["a","b"])
    def test_atomic_item_decisions_provenance_and_revert(self):
        result=apply_batch(cluster=self.cluster,priority_path=self.pp,book_path=self.bp,decisions_path=self.dp,action="accept_vision",confirmed=True)
        self.assertTrue(result["applied"])
        doc=review_decisions.load_json(self.dp); self.assertEqual({x["item_id"] for x in doc["decisions"]},{"a","b","c"})
        self.assertTrue(all(x.get("batch_provenance",{}).get("batch_id")==result["batch_id"] for x in doc["decisions"]))
        self.assertEqual(revert_batch(decisions_path=self.dp,batch_id=result["batch_id"])["reverted_item_count"],3)
        self.assertEqual(review_decisions.load_json(self.dp)["decisions"],[])
    def test_existing_or_stale_member_causes_all_or_nothing(self):
        review_decisions.record_decision(decisions_path=self.dp,priority_path=self.pp,book_path=self.bp,item_id="a",action="keep_surya")
        before=self.dp.read_bytes(); result=apply_batch(cluster=self.cluster,priority_path=self.pp,book_path=self.bp,decisions_path=self.dp,action="keep_surya",confirmed=True)
        self.assertFalse(result["applied"]); self.assertEqual(before,self.dp.read_bytes())

    def test_stale_cluster_evidence_and_exclusion_are_fail_closed(self):
        changed = json.loads(self.pp.read_text(encoding="utf8")); changed["items"][1]["vision"] = "другое"
        self.pp.write_text(json.dumps(changed), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "stale"):
            apply_batch(cluster=self.cluster, priority_path=self.pp, book_path=self.bp, decisions_path=self.dp, action="ignore", confirmed=True)
        self.assertFalse(self.dp.exists())
        with self.assertRaisesRegex(ValueError, "two members"):
            preview(self.cluster, {x["item_id"]:x for x in self.priority["items"]}, "ignore", ["b", "c"])

    def test_workspace_preview_exclusion_confirmation_and_metrics(self):
        workspace = ReviewWorkspace(self.root)
        plan = workspace.batch_preview(self.cluster["cluster_id"], "ignore", ["c"])
        self.assertEqual(plan["member_ids"], ["a", "b"])
        with self.assertRaisesRegex(ValueError, "confirmation"):
            workspace.batch_confirm(self.cluster["cluster_id"], "ignore", ["c"])
        result = workspace.batch_confirm(self.cluster["cluster_id"], "ignore", ["c"], confirmed=True)
        self.assertTrue(result["applied"])
        self.assertEqual(workspace.progress()["confirmed_batch_operations"], 1)
        self.assertEqual(workspace.progress()["items_resolved_via_batch"], 2)
        with self.assertRaisesRegex(ValueError, "unresolved"):
            workspace.batch_preview(self.cluster["cluster_id"], "ignore", ["c"])


if __name__ == "__main__": unittest.main()
