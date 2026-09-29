import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.export_document import export_document


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACAQAAAABazTCJAAAAIGNIUk0AAHomAACAhAAA+gAAAIDoAAB1MAAA6mAAADqYAAAXcJy6UTwAAAACYktHRAAB3YoTpAAAAAd0SU1FB+oJHAcpDsplvHsAAAAldEVYdGRhdGU6Y3JlYXRlADIwMjYtMDktMjhUMDc6NDE6MTQrMDA6MDAdjJROAAAAJXRFWHRkYXRlOm1vZGlmeQAyMDI2LTA5LTI4VDA3OjQxOjE0KzAwOjAwbNEs8gAAACh0RVh0ZGF0ZTp0aW1lc3RhbXAAMjAyNi0wOS0yOFQwNzo0MToxNCswMDowMDvEDS0AAAAMSURBVAjXYzjAcAAAAwQBgcU7oqkAAAAASUVORK5CYII=")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str): path.write_text(value, encoding="utf-8")
    else: path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class ExportDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        (self.root / "images").mkdir(); (self.root / "images/figure.png").write_bytes(PNG)
        for table in ("page-0313-table-01", "page-0313-table-02", "page-0320-table-01", "page-0321-table-01"):
            write(self.root / f"tables/{table}.json", {"grid":[["A", "B"], ["1", "2"]]})
            write(self.root / f"tables/{table}.tsv", "A\tB\n1\t2\n"); write(self.root / f"tables/{table}.html", "<table><tr><td>A</td></tr></table>")
        self.document = {"schema_version":"1.0","artifact_type":"booktext.canonical_document","document_id":"d","metadata":{"booktext_version":"0.1.0","title":None},"source_pages":[{"source_page":1},{"source_page":313},{"source_page":319},{"source_page":320},{"source_page":321}],"source":{},"review":{"unresolved_priority_counts":{"HIGH":1,"MEDIUM":2}},"warnings":[],"notes":[],"blocks":[
            {"block_id":"h","type":"heading","reading_order":1,"text":"Heading & <safe>","level":None,"provenance":{"source_page":1}},
            {"block_id":"p","type":"paragraph","reading_order":2,"text":"Corrected prose","provenance":{"source_page":1,"correction_decision_ids":["decision-1"]}},
            {"block_id":"u","type":"unknown","reading_order":3,"text":"Unclassified content","provenance":{"source_page":1}},
            {"block_id":"f","type":"figure","reading_order":4,"image":"images/figure.png","description":"Auxiliary","caption_id":None,"provenance":{"source_page":1}},
            {"block_id":"t1","type":"table","reading_order":5,"table_status":"structured","artifacts":{"json":"tables/page-0313-table-01.json","tsv":"tables/page-0313-table-01.tsv","html":"tables/page-0313-table-01.html"},"provenance":{"source_page":313}},
            {"block_id":"t2","type":"table","reading_order":6,"table_status":"structured","artifacts":{"json":"tables/page-0313-table-02.json","tsv":"tables/page-0313-table-02.tsv","html":"tables/page-0313-table-02.html"},"provenance":{"source_page":313}},
            {"block_id":"fallback","type":"table","reading_order":7,"table_status":"fallback_review","review_required":True,"provenance":{"source_page":319}},
            {"block_id":"t3","type":"table","reading_order":8,"table_status":"structured","artifacts":{"json":"tables/page-0320-table-01.json","tsv":"tables/page-0320-table-01.tsv","html":"tables/page-0320-table-01.html"},"provenance":{"source_page":320}},
            {"block_id":"t4","type":"table","reading_order":9,"table_status":"structured","artifacts":{"json":"tables/page-0321-table-01.json","tsv":"tables/page-0321-table-01.tsv","html":"tables/page-0321-table-01.html"},"provenance":{"source_page":321}},
        ]}
        write(self.root / "book_document.json", self.document)
        write(self.root / "review_decisions.json", {"decisions":[{"decision_id":"decision-1"}]})

    def tearDown(self): self.temp.cleanup()

    def test_all_exports_are_valid_and_conservative(self):
        before = (self.root / "book_document.json").read_bytes()
        manifest = export_document(self.root, ["all"])
        self.assertEqual(before, (self.root / "book_document.json").read_bytes())
        self.assertEqual(set(manifest["generated"]), {"markdown","html","docx","epub","pdf"})
        self.assertTrue(manifest["generated"]["docx"]["named_deliverable"].startswith("deliverables/"))
        self.assertTrue((self.root / manifest["generated"]["docx"]["named_deliverable"]).is_file())
        self.assertEqual(manifest["fallback_unknown_counts"], {"unknown":1,"fallback_tables":1})
        markdown = (self.root / "exports/book.md").read_text(encoding="utf-8")
        html = (self.root / "exports/book.html").read_text(encoding="utf-8")
        self.assertIn("Corrected prose", markdown); self.assertIn("Unknown layout block", markdown); self.assertIn("fallback review", markdown)
        self.assertEqual(markdown.count("| A | B |"), 4)
        self.assertIn("Heading &amp; &lt;safe&gt;", html); self.assertIn("Table fallback review", html)
        with zipfile.ZipFile(self.root / "exports/book.docx") as archive: self.assertIn("word/document.xml", archive.namelist())
        with zipfile.ZipFile(self.root / "exports/book.epub") as archive:
            self.assertIn("mimetype", archive.namelist()); self.assertTrue(any(name.endswith(".png") for name in archive.namelist()))
        self.assertGreater((self.root / "exports/book.pdf").stat().st_size, 100)
        first_markdown = markdown; first_html = html
        with zipfile.ZipFile(self.root / "exports/book.docx") as archive: first_docx_xml = archive.read("word/document.xml")
        with zipfile.ZipFile(self.root / "exports/book.epub") as archive: first_epub_xhtml = archive.read("EPUB/text/ch001.xhtml")
        export_document(self.root, ["all"])
        self.assertEqual(first_markdown, (self.root / "exports/book.md").read_text(encoding="utf-8"))
        self.assertEqual(first_html, (self.root / "exports/book.html").read_text(encoding="utf-8"))
        with zipfile.ZipFile(self.root / "exports/book.docx") as archive: self.assertEqual(first_docx_xml, archive.read("word/document.xml"))
        with zipfile.ZipFile(self.root / "exports/book.epub") as archive: self.assertEqual(first_epub_xhtml, archive.read("EPUB/text/ch001.xhtml"))

    def test_invalid_cdm_or_broken_table_fails_without_export_success(self):
        self.document["blocks"][4]["artifacts"]["json"] = "tables/missing.json"; write(self.root / "book_document.json", self.document)
        with self.assertRaisesRegex(ValueError, "missing structured"):
            export_document(self.root, ["markdown"])


if __name__ == "__main__": unittest.main()
