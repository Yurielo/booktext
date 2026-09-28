import json
import tempfile
import unittest
from pathlib import Path

from tools import review_decisions
from tools.canonical_document import build_document, validate_document, write_document


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class CanonicalDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "pages").mkdir(); (self.root / "images").mkdir(); (self.root / "tables").mkdir()
        for name in ("pages/p1.png", "pages/p2.png", "pages/p3.png", "images/figure.png"):
            (self.root / name).write_bytes(b"asset")
        for name, value in (("tables/page-0001-table-01.json", {"grid":[["a"]]}), ("tables/page-0001-table-01.tsv", "a\n"), ("tables/page-0001-table-01.html", "<table><tr><td>a</td></tr></table>")):
            write(self.root / name, value)
        self.book = {"ocr_engine":"surya", "ocr_languages":"rus+eng", "sources":["/private/source.pdf"], "pages":[
            {"page":1,"text":"alpha foo","page_image":"pages/p1.png","visuals":[],"tables":{"tables":[{"index":1,"status":"HTML_TABLE","rows":1,"columns":1,"json":"page-0001-table-01.json","tsv":"page-0001-table-01.tsv","html":"page-0001-table-01.html"}]}},
            {"page":2,"text":"Heading body","page_image":"pages/p2.png","visuals":[{"index":1,"type":"image","image":"images/figure.png","description":"auxiliary","description_file":None}],"tables":{"tables":[]}},
            {"page":3,"text":"ambiguous table","page_image":"pages/p3.png","visuals":[],"tables":{"tables":[{"index":1,"status":"fallback_review","diagnostic":"image_only"}]}},
        ]}
        self.priority = {"schema_version":"1.0","artifact_type":"booktext.review_priority","counts":{},"items":[{"item_id":"change","page":1,"priority":"MEDIUM","item_type":"ocr_qa","surya":"foo","vision":"bar","reasons":[],"signals":{}}]}
        write(self.root / "book.json", self.book); write(self.root / "review_priority.json", self.priority)
        review_decisions.record_decision(decisions_path=self.root / "review_decisions.json", priority_path=self.root / "review_priority.json", book_path=self.root / "book.json", item_id="change", action="accept_vision")
        write(self.root / "surya/surya-input/results.json", {"pages":[
            {"page":1,"blocks":[{"label":"Text","reading_order":1,"html":"<p>alpha foo</p>"},{"label":"Table","reading_order":2,"html":"<table><tr><td>a</td></tr></table>"}]},
            {"page":2,"blocks":[{"label":"SectionHeader","reading_order":1,"html":"<h1>Heading</h1>"},{"label":"Text","reading_order":2,"html":"<p>body</p>"}]},
            {"page":3,"blocks":[{"label":"Table","reading_order":1,"html":"<img>"}]},
        ]})

    def tearDown(self): self.temp.cleanup()

    def test_build_is_deterministic_conservative_and_corrected(self):
        first, second = build_document(self.root), build_document(self.root)
        self.assertEqual(first, second)
        validate_document(first, root=self.root, decisions=review_decisions.load_json(self.root / "review_decisions.json"))
        corrected = [b for b in first["blocks"] if b["provenance"].get("source_page") == 1 and b["type"] == "unknown"][0]
        self.assertEqual(corrected["text"], "alpha bar")
        self.assertEqual(len(corrected["provenance"]["correction_decision_ids"]), 1)
        headings = [b for b in first["blocks"] if b["type"] == "heading"]
        self.assertEqual(headings[0]["text"], "Heading"); self.assertIsNone(headings[0]["level"])
        tables = [b for b in first["blocks"] if b["type"] == "table"]
        self.assertEqual(tables[0]["table_status"], "structured")
        self.assertEqual(set(tables[0]["artifacts"]), {"json","tsv","html"})
        self.assertEqual(tables[1]["table_status"], "fallback_review")
        self.assertNotIn("cells", tables[1])
        figure = next(b for b in first["blocks"] if b["type"] == "figure")
        self.assertIsNone(figure["caption_id"])
        self.assertEqual([b["reading_order"] for b in first["blocks"]], list(range(1, len(first["blocks"]) + 1)))
        self.assertEqual(write_document(first, self.root).name, "book_document.json")

    def test_validator_rejects_broken_references_duplicate_ids_and_decisions(self):
        document = build_document(self.root)
        broken = json.loads(json.dumps(document)); broken["blocks"][1]["block_id"] = broken["blocks"][0]["block_id"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_document(broken, root=self.root, decisions=review_decisions.load_json(self.root / "review_decisions.json"))
        broken = json.loads(json.dumps(document)); table = next(b for b in broken["blocks"] if b["type"] == "table" and b["table_status"] == "structured"); table["artifacts"]["json"] = "tables/missing.json"
        with self.assertRaisesRegex(ValueError, "missing structured"):
            validate_document(broken, root=self.root, decisions=review_decisions.load_json(self.root / "review_decisions.json"))
        broken = json.loads(json.dumps(document)); corrected = next(b for b in broken["blocks"] if b["provenance"].get("correction_decision_ids")); corrected["provenance"]["correction_decision_ids"] = ["gone"]
        with self.assertRaisesRegex(ValueError, "decision provenance"):
            validate_document(broken, root=self.root, decisions=review_decisions.load_json(self.root / "review_decisions.json"))

    def test_multiple_tables_order_and_schema_reading_order_rejection(self):
        for suffix, value in (("json", {"grid":[["b"]]}), ("tsv", "b\n"), ("html", "<table><tr><td>b</td></tr></table>")):
            write(self.root / f"tables/page-0001-table-02.{suffix}", value)
        self.book["pages"][0]["tables"]["tables"].append({"index":2,"status":"HTML_TABLE","rows":1,"columns":1,"json":"page-0001-table-02.json","tsv":"page-0001-table-02.tsv","html":"page-0001-table-02.html"})
        write(self.root / "book.json", self.book)
        decisions = review_decisions.load_json(self.root / "review_decisions.json")
        decisions["book_source"]["sha256"] = review_decisions.sha256_file(self.root / "book.json")
        write(self.root / "review_decisions.json", decisions)
        raw = json.loads((self.root / "surya/surya-input/results.json").read_text())
        raw["pages"][0]["blocks"].append({"label":"Table","reading_order":3,"html":"<table><tr><td>b</td></tr></table>"})
        write(self.root / "surya/surya-input/results.json", raw)
        document = build_document(self.root)
        tables = [block for block in document["blocks"] if block["type"] == "table" and block["provenance"]["source_page"] == 1]
        self.assertEqual([block["provenance"]["table_index"] for block in tables], [1, 2])
        broken = json.loads(json.dumps(document)); broken["schema_version"] = "bad"
        with self.assertRaisesRegex(ValueError, "schema"):
            validate_document(broken, root=self.root, decisions=decisions)
        broken = json.loads(json.dumps(document)); broken["blocks"][1]["reading_order"] = 99
        with self.assertRaisesRegex(ValueError, "reading order"):
            validate_document(broken, root=self.root, decisions=decisions)

    def test_missing_layout_preserves_page_text_as_unknown(self):
        (self.root / "surya/surya-input/results.json").unlink()
        document = build_document(self.root)
        self.assertIn("ambiguous table", [b.get("text") for b in document["blocks"] if b["type"] == "unknown"])


if __name__ == "__main__": unittest.main()
