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
            {"page":2,"blocks":[{"label":"PageHeader","reading_order":1,"html":"<p>46</p>"},{"label":"PageHeader","reading_order":2,"html":"<p>Parasites and contaminants</p>"},{"label":"SectionHeader","reading_order":3,"html":"<h1>Heading</h1>"},{"label":"Text","reading_order":4,"html":"<p>body</p>"}]},
            {"page":3,"blocks":[{"label":"Table","reading_order":1,"html":"<img>"}]},
        ]})

    def tearDown(self): self.temp.cleanup()

    def test_build_is_deterministic_conservative_and_corrected(self):
        first, second = build_document(self.root), build_document(self.root)
        self.assertEqual(first, second)
        self.assertEqual(first["metadata"]["title"], "source")
        validate_document(first, root=self.root, decisions=review_decisions.load_json(self.root / "review_decisions.json"))
        corrected = [b for b in first["blocks"] if b["provenance"].get("source_page") == 1 and b["type"] == "paragraph"][0]
        self.assertEqual(corrected["text"], "alpha bar")
        self.assertEqual(len(corrected["provenance"]["correction_decision_ids"]), 1)
        headings = [b for b in first["blocks"] if b["type"] == "heading"]
        self.assertEqual(headings[0]["text"], "Heading"); self.assertIsNone(headings[0]["level"])
        publication_text = "\n".join(block.get("text", "") for block in first["blocks"])
        self.assertNotIn("Parasites and contaminants", publication_text)
        self.assertNotIn("\n46\n", f"\n{publication_text}\n")
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

    def test_missing_layout_projects_plain_text_as_paragraphs(self):
        (self.root / "surya/surya-input/results.json").unlink()
        document = build_document(self.root)
        self.assertIn("ambiguous table", [b.get("text") for b in document["blocks"] if b["type"] == "paragraph"])

    def test_missing_layout_only_promotes_strong_textual_headings(self):
        (self.root / "surya/surya-input/results.json").unlink()
        self.book["pages"] = [{"page": 1, "text": "DOCUMENT TITLE\n\nOrdinary prose.\n\nArticle 12\n\nMore prose."}]
        self.priority["items"] = []
        write(self.root / "book.json", self.book); write(self.root / "review_priority.json", self.priority)
        document = build_document(self.root)
        page_blocks = [block for block in document["blocks"] if block["type"] != "page_break"]
        self.assertEqual([block["type"] for block in page_blocks], ["heading", "paragraph", "heading", "paragraph"])
        self.assertEqual([block["text"] for block in page_blocks], ["DOCUMENT TITLE", "Ordinary prose.", "Article 12", "More prose."])

    def test_legacy_page_text_removes_only_repeated_boundary_running_matter(self):
        (self.root / "surya/surya-input/results.json").unlink()
        (self.root / "review_decisions.json").unlink()
        self.book["pages"] = [
            {"page": number, "text": f"{number}\nParasites and contaminants\nBody text {number}"}
            for number in (46, 47, 48)
        ]
        self.priority["items"] = []
        write(self.root / "book.json", self.book); write(self.root / "review_priority.json", self.priority)
        document = build_document(self.root)
        values = [block["text"] for block in document["blocks"] if block["type"] == "paragraph"]
        self.assertEqual(values, ["Body text 46", "Body text 47", "Body text 48"])

    def test_recovered_visual_crop_becomes_a_figure_with_evidence_provenance(self):
        (self.root / "images/recovered.png").write_bytes(b"crop")
        write(self.root / "visual_evidence.json", {
            "schema_version": "1.0", "artifact_type": "booktext.recovered_visual_evidence",
            "visuals": [{"page": 1, "index": 1, "type": "diagram", "image": "images/recovered.png", "bbox": [.1, .2, .9, .8],
                         "source_provenance": {"kind": "preserved_surya_layout_visual_crop", "ocr_performed": False}}],
        })
        document = build_document(self.root)
        figure = next(block for block in document["blocks"] if block.get("image") == "images/recovered.png")
        self.assertEqual(figure["type"], "figure")
        self.assertEqual(figure["provenance"]["source"]["kind"], "preserved_surya_layout_visual_crop")
        self.assertEqual(document["source"]["recovered_visual_evidence"]["path"], "visual_evidence.json")

    def test_empty_native_layout_does_not_drop_ocr_text_and_warnings(self):
        (self.root / "surya/surya-input/results.json").unlink()
        (self.root / "review_decisions.json").unlink()
        self.book["pages"] = [{"page":1, "text":"Scanned text survives.", "layout_blocks":[],
                               "extraction_warnings":[{"page":1,"kind":"source_render_diagnostic"}]}]
        write(self.root / "book.json", self.book)
        doc = build_document(self.root)
        self.assertIn("Scanned text survives.", [block.get("text") for block in doc["blocks"]])
        self.assertIn({"page":1,"kind":"source_render_diagnostic"}, doc["warnings"])

    def test_scan_without_native_blocks_preserves_primary_column_order(self):
        (self.root / "review_decisions.json").unlink()
        self.book["pages"] = [{"page": 1, "text": "Left top. Left bottom. Right top.",
                               "layout_blocks": [], "visuals": []}]
        write(self.root / "book.json", self.book)
        write(self.root / "surya/surya-input/results.json", {"pages": [{"page": 1, "blocks": [
            {"label": "Text", "reading_order": 1, "bbox": [0, 0, 40, 20], "html": "<p>Left top.</p>"},
            {"label": "Text", "reading_order": 2, "bbox": [0, 80, 40, 100], "html": "<p>Left bottom.</p>"},
            {"label": "Text", "reading_order": 3, "bbox": [60, 0, 100, 20], "html": "<p>Right top.</p>"},
        ]}]})
        document = build_document(self.root)
        self.assertEqual([b["text"] for b in document["blocks"] if b["type"] == "paragraph"],
                         ["Left top.", "Left bottom.", "Right top."])

    def test_native_two_column_mapping_becomes_verified_structured_native_text_table(self):
        from PIL import Image
        (self.root / "surya/surya-input/results.json").unlink()
        (self.root / "review_decisions.json").unlink()
        Image.new("RGB", (500, 500), "white").save(self.root / "pages/p1.png")
        def line(text, x0, y0, x1, y1):
            return {"text": text, "bbox": [x0, y0, x1, y1], "words": [{"text": text, "bbox": [x0, y0, x1, y1]}]}
        lines = [line("The symbols are:", 10, 10, 70, 16)]
        for offset, left, right in ((20, "of Saturn", "triangle"), (30, "of Jupiter", "quadrangle"), (40, "of Mars", "pentagon")):
            lines.extend([line(left, 20, offset, 46, offset + 6), line(right, 62, offset, 96, offset + 6)])
        lines.append(line("of Moon a nonagon", 20, 50, 96, 56))
        lines.append(line("The list continues.", 10, 65, 80, 71))
        self.book["pages"] = [{"page": 1, "text": "The symbols are:", "page_image": "pages/p1.png",
            "source_provenance": {"page_size_points": [100, 100]}, "visuals": [], "tables": {"tables": []},
            "layout_blocks": [{"label": "Text", "extraction": "native_pdf_words", "reading_order": 1,
                               "bbox": [10, 10, 96, 71], "lines": lines,
                               "text": "\n".join(item["text"] for item in lines)}]}]
        self.priority["items"] = []
        write(self.root / "book.json", self.book); write(self.root / "review_priority.json", self.priority)
        document = build_document(self.root)
        table = next(block for block in document["blocks"] if block.get("table_status") == "structured")
        self.assertNotIn("cells", table)
        self.assertEqual(table["rows"], 4)
        self.assertEqual(table["columns"], 2)
        grid = json.loads((self.root / table["artifacts"]["json"]).read_text(encoding="utf-8"))["grid"]
        self.assertEqual(grid, [["of Saturn", "triangle"], ["of Jupiter", "quadrangle"], ["of Mars", "pentagon"], ["of Moon a nonagon", ""]])
        self.assertEqual(table["provenance"]["source"]["cell_status"], "verified_native_text_geometry")
        publication = "\n".join(block.get("text", "") for block in document["blocks"])
        self.assertIn("The symbols are:", publication)
        self.assertIn("The list continues.", publication)
        self.assertNotIn("Saturn\ntriangle", publication)
        validate_document(document, root=self.root, decisions={"decisions": []})

    def test_scan_figure_stays_at_anchored_primary_position_once(self):
        (self.root / "review_decisions.json").unlink()
        self.book["pages"] = [{"page": 1, "text": "Left caption. Right column.", "layout_blocks": [],
            "visuals": [{"index": 2, "image": "images/figure.png", "bbox": [0, 30, 40, 100],
                         "source_provenance": {"kind": "surya_layout_source_crop", "raw_layout": "surya/surya-input/results.json"}}]}]
        write(self.root / "book.json", self.book)
        write(self.root / "surya/surya-input/results.json", {"pages": [{"page": 1, "blocks": [
            {"label": "Text", "reading_order": 0, "bbox": [0, 0, 40, 20], "html": "<p>Left caption.</p>"},
            {"label": "Figure", "reading_order": 1, "bbox": [0, 30, 40, 100], "html": "", "skipped": True},
            {"label": "Text", "reading_order": 2, "bbox": [60, 0, 100, 20], "html": "<p>Right column.</p>"},
        ]}]})
        document = build_document(self.root)
        self.assertEqual([b["type"] for b in document["blocks"]], ["paragraph", "figure", "paragraph", "page_break"])

    def test_v2_extraction_evidence_is_verified_before_offline_build(self):
        import hashlib
        self.book["extraction_policy"] = "automatic-extraction-2"
        write(self.root / "book.json", self.book)
        write(self.root / "extraction/source.json", {})
        write(self.root / "extraction/native.xml", "preserved")
        write(self.root / "extraction/artifacts.json", {"extraction/native.xml": hashlib.sha256(b"preserved").hexdigest()})
        build_document(self.root)
        write(self.root / "extraction/native.xml", "changed")
        with self.assertRaisesRegex(ValueError, "stale extraction evidence"):
            build_document(self.root)


if __name__ == "__main__": unittest.main()
