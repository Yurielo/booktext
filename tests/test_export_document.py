import base64
import json
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from tools.export_document import export_document, export_translation, render_markdown, _enable_docx_text_wrapping, _format_wide_docx_tables, WP, W, A
from tools import translation


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACAQAAAABazTCJAAAAIGNIUk0AAHomAACAhAAA+gAAAIDoAAB1MAAA6mAAADqYAAAXcJy6UTwAAAACYktHRAAB3YoTpAAAAAd0SU1FB+oJHAcpDsplvHsAAAAldEVYdGRhdGU6Y3JlYXRlADIwMjYtMDktMjhUMDc6NDE6MTQrMDA6MDAdjJROAAAAJXRFWHRkYXRlOm1vZGlmeQAyMDI2LTA5LTI4VDA3OjQxOjE0KzAwOjAwbNEs8gAAACh0RVh0ZGF0ZTp0aW1lc3RhbXAAMjAyNi0wOS0yOFQwNzo0MToxNCswMDowMDvEDS0AAAAMSURBVAjXYzjAcAAAAwQBgcU7oqkAAAAASUVORK5CYII=")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str): path.write_text(value, encoding="utf-8")
    else: path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class ExportDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        (self.root / "images").mkdir(); (self.root / "images/figure.png").write_bytes(PNG)
        from PIL import Image
        Image.new("RGB", (2, 2), "red").save(self.root / "images/fallback.png")
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
            {"block_id":"fallback","type":"table","reading_order":7,"table_status":"fallback_review","review_required":True,"fallback_visual":"images/fallback.png","provenance":{"source_page":319}},
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
        self.assertEqual(set(manifest["generated"]), {"markdown","html","docx","epub","pdf","pdf_image"})
        self.assertTrue(manifest["generated"]["docx"]["named_deliverable"].startswith("deliverables/"))
        self.assertTrue((self.root / manifest["generated"]["docx"]["named_deliverable"]).is_file())
        self.assertEqual(manifest["fallback_unknown_counts"], {"unknown":1,"fallback_tables":1})
        markdown = (self.root / "exports/book.md").read_text(encoding="utf-8")
        html = (self.root / "exports/book.html").read_text(encoding="utf-8")
        self.assertIn("Corrected prose", markdown); self.assertIn("Unclassified content", markdown); self.assertNotIn("Unknown layout block", markdown); self.assertNotIn("fallback review", markdown)
        self.assertIn("fallback.png", markdown)
        self.assertNotIn("\n---\n", markdown)
        self.assertEqual(markdown.count("| A | B |"), 4)
        self.assertIn("Heading &amp; &lt;safe&gt;", html); self.assertIn("Unclassified content", html); self.assertNotIn("Table fallback review", html); self.assertNotIn("<hr", html)
        with zipfile.ZipFile(self.root / "exports/book.docx") as archive:
            self.assertIn("word/document.xml", archive.namelist())
            self.assertTrue(any(name.startswith("word/media/") for name in archive.namelist()))
            xml = archive.read("word/document.xml")
            self.assertIn(b"wrapSquare", xml)
            self.assertIn(b'allowOverlap="0"', xml)
            self.assertNotIn(b'allowOverlap="1"', xml)
            self.assertNotIn(b"Unknown layout", xml)
            self.assertNotIn(b"Table fallback review", xml)
            root = ET.fromstring(xml)
            self.assertFalse(any(node.get("descr") == "Source table" for node in root.findall(f".//{WP}docPr")))
            self.assertEqual(1, len(root.findall(f".//{WP}inline")))
            self.assertEqual(1, len(root.findall(f".//{WP}anchor")))
        with zipfile.ZipFile(self.root / "exports/book.epub") as archive:
            self.assertIn("mimetype", archive.namelist()); self.assertTrue(any(name.endswith(".png") for name in archive.namelist()))
        self.assertGreater((self.root / "exports/book.pdf").stat().st_size, 100)
        self.assertGreater((self.root / "exports/book-image.pdf").stat().st_size, 100)
        # The image-only PDF is rendered page imagery, not a second text layer.
        try:
            import pypdf
            self.assertEqual("", "".join(page.extract_text() or "" for page in pypdf.PdfReader(self.root / "exports/book-image.pdf").pages).strip())
        except ImportError:
            pass
        self.assertTrue(any(item["kind"] == "fallback_table_source_visual_preserved" and item["page"] == 319 for item in manifest["warnings"]))
        first_markdown = markdown; first_html = html
        with zipfile.ZipFile(self.root / "exports/book.docx") as archive: first_docx_xml = archive.read("word/document.xml")
        with zipfile.ZipFile(self.root / "exports/book.epub") as archive: first_epub_xhtml = archive.read("EPUB/text/ch001.xhtml")
        export_document(self.root, ["all"])
        self.assertEqual(first_markdown, (self.root / "exports/book.md").read_text(encoding="utf-8"))
        self.assertEqual(first_html, (self.root / "exports/book.html").read_text(encoding="utf-8"))
        with zipfile.ZipFile(self.root / "exports/book.docx") as archive: self.assertEqual(first_docx_xml, archive.read("word/document.xml"))
        with zipfile.ZipFile(self.root / "exports/book.epub") as archive: self.assertEqual(first_epub_xhtml, archive.read("EPUB/text/ch001.xhtml"))

    def test_source_footnote_marker_is_not_reinterpreted_as_a_list(self):
        document = json.loads(json.dumps(self.document))
        document["blocks"] = [{"block_id": "p", "type": "paragraph", "reading_order": 1,
                               "text": "* Source footnote marker remains prose.", "provenance": {"source_page": 1}}]
        self.assertIn("\\* Source footnote marker", render_markdown(document, self.root))

    def test_explicit_selection_never_defaults_to_all_or_leaves_helper_markdown(self):
        with self.assertRaisesRegex(ValueError, "select at least one export format"):
            export_document(self.root, [])
        manifest = export_document(self.root, ["docx", "pdf_image", "docx"])
        self.assertEqual(set(manifest["generated"]), {"docx", "pdf_image"})
        self.assertTrue((self.root / "exports/book.docx").is_file())
        self.assertTrue((self.root / "exports/book-image.pdf").is_file())
        self.assertFalse((self.root / "exports/book.md").exists())
        self.assertFalse((self.root / "exports/.book-docx-input.md").exists())
        self.assertFalse((self.root / "exports/.book-image-source.pdf").exists())

    def test_large_illustrations_remain_inline_and_fit_the_printable_page(self):
        path = self.root / "layout.docx"
        root = ET.Element(f"{W}document")
        body = ET.SubElement(root, f"{W}body")
        for size in ((1000000, 1500000), (15000000, 20000000)):
            drawing = ET.SubElement(ET.SubElement(ET.SubElement(body, f"{W}p"), f"{W}r"), f"{W}drawing")
            inline = ET.SubElement(drawing, f"{WP}inline")
            ET.SubElement(inline, f"{WP}extent", {"cx":str(size[0]), "cy":str(size[1])})
            ET.SubElement(ET.SubElement(inline, f"{A}xfrm"), f"{A}ext", {"cx":str(size[0]), "cy":str(size[1])})
        with zipfile.ZipFile(path,"w") as archive:
            archive.writestr("word/document.xml", ET.tostring(root))
        _enable_docx_text_wrapping(path)
        with zipfile.ZipFile(path) as archive: root = ET.fromstring(archive.read("word/document.xml"))
        self.assertEqual(len(root.findall(f".//{WP}anchor")), 1)
        self.assertEqual(len(root.findall(f".//{WP}inline")), 1)
        extent = root.find(f".//{WP}inline/{WP}extent")
        self.assertLessEqual(int(extent.get("cx")), 5943600)
        self.assertLessEqual(int(extent.get("cy")), 8077200)
        self.assertAlmostEqual(int(extent.get("cx"))/int(extent.get("cy")), .75, places=5)
        self.assertEqual(root.find(f".//{WP}inline/{A}xfrm/{A}ext").attrib, extent.attrib)
        self.assertIsNotNone(root.find(f'.//{W}br[@{W}clear="all"]'))

    def test_translation_export_uses_valid_anchored_derivative_without_mutating_cdm(self):
        before = (self.root / "book_document.json").read_bytes()
        entries = []
        for block in self.document["blocks"]:
            if block["type"] not in {"heading", "paragraph", "list", "caption", "unknown"} or not block.get("text"): continue
            text = block["text"]
            entries.append({"block_id": block["block_id"], "source_text_sha256": __import__("hashlib").sha256(text.encode()).hexdigest(),
                            "status": "machine_translated", "translation": "Перевод: " + text})
        translation_manifest = {"schema_version": "1.0", "artifact_type": "booktext.translation", "translation_id": "translation-test",
            "source": {"cdm_sha256": __import__("hashlib").sha256(before).hexdigest()}, "provider": {"model": "test"}, "blocks": entries}
        write(self.root / "translations/en-ru.json", translation_manifest)
        manifest = export_translation(self.root, ["markdown", "docx"], source_language="en", target_language="ru")
        self.assertEqual(before, (self.root / "book_document.json").read_bytes())
        self.assertEqual("booktext.translation_export_manifest", manifest["artifact_type"])
        self.assertEqual("translations/en-ru.json", manifest["translation"]["path"])
        self.assertTrue((self.root / "translations/exports/en-ru/book-en-ru.docx").is_file())
        markdown = (self.root / "translations/exports/en-ru/book-en-ru.md").read_text(encoding="utf-8")
        self.assertIn("Перевод: Corrected prose", markdown)
        self.assertNotIn("Unknown layout block", markdown)

    def test_archived_qwen_translation_exports_without_model_inference(self):
        source = (self.root / "book_document.json").read_bytes()
        entries = [{"block_id": block["block_id"], "source_text_sha256": translation._sha_bytes(block["text"].encode()),
                    "status": "machine_translated", "translation": "Legacy " + block["text"]}
                   for block in self.document["blocks"] if block["type"] in translation.TEXT_TYPES and block.get("text")]
        manifest = {"schema_version": "1.0", "artifact_type": "booktext.translation", "translation_id": "legacy-qwen",
                    "direction": {"source_language": "en", "target_language": "ru"}, "source": {"cdm_sha256": translation._sha_bytes(source)},
                    "provider": {"model": "qwen3:8b"}, "blocks": entries}
        revision = translation._digest(manifest)
        write(self.root / "translations/history" / f"en-ru-{revision}.json", manifest)
        with mock.patch("tools.translation._model_info") as inference:
            result = export_translation(self.root, ["markdown"], source_language="en", target_language="ru", history_sha=revision)
        inference.assert_not_called()
        self.assertEqual(f"translations/history/en-ru-{revision}.json", result["translation"]["path"])
        self.assertIn("Legacy Corrected prose", (self.root / "translations/exports/en-ru/history" / revision / "book-en-ru.md").read_text())

    def test_wide_docx_table_widths_preserve_every_cell_and_are_idempotent(self):
        path = self.root / "wide.docx"
        root = ET.Element(f"{W}document"); body = ET.SubElement(root, f"{W}body")
        table = ET.SubElement(body, f"{W}tbl"); grid = ET.SubElement(table, f"{W}tblGrid")
        for _ in range(11): ET.SubElement(grid, f"{W}gridCol", {f"{W}w": "850"})
        content = [["Disease description", *["Year"]*10], ["Meningococcal infection and complications", *["12,671"]*10]]
        for values in content:
            row = ET.SubElement(table, f"{W}tr")
            for value in values:
                cell = ET.SubElement(row, f"{W}tc")
                ET.SubElement(ET.SubElement(ET.SubElement(cell, f"{W}p"), f"{W}r"), f"{W}t").text = value
        with zipfile.ZipFile(path, "w") as archive: archive.writestr("word/document.xml", ET.tostring(root))
        self.assertEqual(_format_wide_docx_tables(path), 1)
        with zipfile.ZipFile(path) as archive: first = archive.read("word/document.xml")
        result = ET.fromstring(first)
        widths = [int(node.get(f"{W}w")) for node in result.findall(f".//{W}gridCol")]
        self.assertEqual(sum(widths), 9360)
        self.assertGreater(widths[0], widths[1]*3)
        self.assertEqual([node.text for node in result.iter(f"{W}t")], [value for row in content for value in row])
        self.assertEqual([int(node.get(f"{W}w")) for node in result.findall(f".//{W}tcW")], widths*2)
        self.assertEqual(result.find(f".//{W}tbl")[0].tag, f"{W}tblPr")
        self.assertTrue(all(node[0].tag == f"{W}tcPr" for node in result.iter(f"{W}tc")))
        self.assertEqual(_format_wide_docx_tables(path), 1)
        with zipfile.ZipFile(path) as archive: self.assertEqual(first, archive.read("word/document.xml"))

    def test_small_or_merged_docx_tables_are_not_reinterpreted(self):
        path = self.root / "complex.docx"
        root = ET.Element(f"{W}document"); body = ET.SubElement(root, f"{W}body")
        for count, merged in [(2, False), (8, True)]:
            table = ET.SubElement(body, f"{W}tbl"); grid = ET.SubElement(table, f"{W}tblGrid")
            for _ in range(count): ET.SubElement(grid, f"{W}gridCol")
            row = ET.SubElement(table, f"{W}tr")
            for _ in range(count):
                cell = ET.SubElement(row, f"{W}tc")
                if merged: ET.SubElement(ET.SubElement(cell, f"{W}tcPr"), f"{W}gridSpan", {f"{W}val":"2"})
        with zipfile.ZipFile(path, "w") as archive: archive.writestr("word/document.xml", ET.tostring(root))
        before = path.read_bytes()
        self.assertEqual(_format_wide_docx_tables(path), 0)
        self.assertEqual(before, path.read_bytes())

    def test_invalid_cdm_or_broken_table_fails_without_export_success(self):
        self.document["blocks"][4]["artifacts"]["json"] = "tables/missing.json"; write(self.root / "book_document.json", self.document)
        with self.assertRaisesRegex(ValueError, "missing structured"):
            export_document(self.root, ["markdown"])

    def test_docx_never_treats_a_prepared_source_page_as_a_figure(self):
        self.document["blocks"] = [block for block in self.document["blocks"] if block["type"] != "figure"]
        next(block for block in self.document["blocks"] if block["block_id"] == "fallback").pop("fallback_visual")
        self.document["blocks"].insert(0, {"block_id": "break", "type": "page_break", "reading_order": 0, "provenance": {"source_page": 1}})
        for order, block in enumerate(self.document["blocks"], start=1):
            block["reading_order"] = order
        (self.root / "pages").mkdir()
        (self.root / "pages/page-0001.png").write_bytes(PNG)
        for source_page in self.document["source_pages"]:
            if source_page["source_page"] == 1:
                source_page["prepared_page"] = "pages/page-0001.png"
        write(self.root / "book_document.json", self.document)
        manifest = export_document(self.root, ["docx"])
        with zipfile.ZipFile(self.root / "exports/book.docx") as archive:
            self.assertFalse(any(name.startswith("word/media/") for name in archive.namelist()))
        self.assertEqual(manifest["visual_preservation"], {
            "mode": "no_visual_evidence", "figure_count": 0,
            "fallback_table_visual_count": 0,
            "policy": "Only CDM figure blocks and explicit fallback-table source crops are embedded. Prepared source pages are provenance, never whole-page figure fallbacks.",
        })
        self.assertTrue(any(item["kind"] == "no_visual_evidence_for_docx" for item in manifest["warnings"]))


if __name__ == "__main__": unittest.main()
