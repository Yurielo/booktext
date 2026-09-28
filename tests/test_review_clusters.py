import json
import tempfile
import unittest
from pathlib import Path

from tools.review_clusters import build_clusters
from tools.review_workspace import ReviewWorkspace


def item(item_id, page, surya, vision, *, priority="MEDIUM", kind="replace", category="text", item_type="ocr_qa"):
    return {"item_id": item_id, "page": page, "priority": priority, "item_type": item_type, "surya": surya, "vision": vision, "reasons": [], "signals": {"qa_kind": kind, "qa_category": category}}


class ReviewClustersTests(unittest.TestCase):
    def test_deterministic_safe_grouping_and_complete_partition(self):
        priority = {"schema_version":"1.0","artifact_type":"booktext.review_priority","items":[
            item("a",1,"слово", "сло во"), item("b",2,"слово", "сло-во"),
            item("c",3,"Ошибка", "ошибка"), item("d",4,"ошибка", "ОШИБКА"),
            item("e",5,"текст", "тэкст"), item("f",6,"текст", "тэкст"),
        ]}
        first, second = build_clusters(priority), build_clusters(priority)
        self.assertEqual(first, second)
        self.assertEqual(sorted(x for c in first["clusters"] for x in c["member_item_ids"]), ["a","b","c","d","e","f"])
        self.assertTrue(any(c["member_count"] == 2 and c["category"] == "segmentation_whitespace" for c in first["clusters"]))
        self.assertTrue(any(c["member_count"] == 2 and c["category"] == "repeated_lexical_pair" for c in first["clusters"]))

    def test_numbers_fractions_units_latin_completeness_and_tables_are_singletons(self):
        priority = {"schema_version":"1.0","artifact_type":"booktext.review_priority","items":[
            item("n1",1,"1.2", "1,2"), item("n2",2,"1.2", "1,2"),
            item("f1",3,"1/2", "1/3"), item("u1",4,"5 mg", "5mg"),
            item("latin",5,"Campylobacter", "Campylobakter"), item("insert",6,"", "lost",kind="insert",category="completeness"),
            item("table",7,"", "",item_type="table_fallback_review"),
        ]}
        result = build_clusters(priority)
        self.assertEqual(len(result["clusters"]), 7)
        self.assertEqual({c["category"] for c in result["clusters"]}, {"numeric_fraction_unit","scientific_or_latin","completeness","structural_fallback"})

    def test_workspace_cluster_mode_derives_member_status_without_propagation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); priority={"schema_version":"1.0","artifact_type":"booktext.review_priority","items":[item("a",1,"текст","тэкст"),item("b",2,"текст","тэкст")]}
            book={"pages":[{"page":1,"text":"текст"},{"page":2,"text":"текст"}]}
            (root/"review_priority.json").write_text(json.dumps(priority),encoding="utf8"); (root/"book.json").write_text(json.dumps(book),encoding="utf8")
            workspace=ReviewWorkspace(root); clusters=workspace.filtered_clusters({})
            self.assertEqual(clusters[0]["member_count"],2)
            workspace.record({"item_id":"a","action":"keep_surya"})
            payload=workspace.cluster_payload(clusters[0]["cluster_id"])
            self.assertEqual(payload["decision_statistics"]["resolved_no_change"],1)
            self.assertEqual(payload["decision_statistics"]["unresolved"],1)
            self.assertEqual(workspace.status_for("b")["status"],"unresolved")


if __name__ == "__main__": unittest.main()
