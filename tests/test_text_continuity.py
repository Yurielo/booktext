import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from tools import canonical_document as cdm, document_continuity as continuity
from tools import export_document as exports, text_normalization as norm
from tools.machine_protocol import document_preview


def block(identifier, page, text, kind="paragraph", geometry=False):
    value = {"block_id": identifier, "type": kind, "text": text,
             "provenance": {"source_page": page, "layout_label": "Text"}}
    if geometry:
        value["provenance"]["line_geometry"] = {
            "source": "native_word_lines", "page_size": [800, 1000],
            "bbox": [100, 60, 700, 900], "first_line": [100, 60, 700, 70],
            "last_line": [100, 890, 700, 900], "line_height": 10, "long_line_ratio": 1}
    return value


class TextContinuityTests(unittest.TestCase):
    def test_unique_discretionary_hyphen_and_single_source_attestation(self):
        options = {"kind": "paragraph", "trusted_lines": True}
        self.assertEqual(norm.normalize("work biomagnetical\u00ad\nly, correctly", **options)[0], "work biomagnetically, correctly")
        self.assertEqual(norm.normalize("In accor-\ndance with rules", words={"accordance": 1}, **options)[0], "In accordance with rules")
        self.assertEqual(norm.normalize("well-\nknown", words={"wellknown": 2, "well-known": 1}, **options)[0], "well-known")
        self.assertEqual(norm.normalize("words-\n\nelsewhere", **options)[0], "words-\n\nelsewhere")
        self.assertEqual(norm.normalize("words\u00ad\n\nelsewhere", **options)[0], "words\u00ad\n\nelsewhere")

    def test_unattested_hard_hyphen_is_not_guessed_and_has_anchored_warning(self):
        raw = "work bioelectrically and biomagnetical-\nly, the rod will grow"
        blocks = [block("rare", 44, raw)]
        report = norm.project(blocks, [raw])
        self.assertIn("biomagnetical-ly", blocks[0]["text"])
        candidate = report["review_candidates"][0]
        self.assertEqual(candidate["block_id"], "rare")
        self.assertEqual(candidate["page"], 44)
        self.assertEqual(candidate["source_text"], "biomagnetical-\nly")
        self.assertEqual(raw[slice(*candidate["source_span"])], candidate["source_text"])
        self.assertEqual(candidate["source_text_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(norm.normalize(blocks[0]["text"], kind="paragraph", trusted_lines=True)[0], blocks[0]["text"])

    def test_typography_does_not_change_initials_numbers_identifiers_or_structure(self):
        raw = "The spirit returns, i. e. to its sphere, e. g. after the work."
        value, reasons = norm.normalize(raw, kind="paragraph", trusted_lines=True)
        self.assertIn("i.e.", value); self.assertIn("e.g.", value)
        self.assertIn("latin_abbreviation_spacing", reasons)
        protected = "Initials I. E. Smith; 12.5 mg; 3/278; URL https://x/i. e. and a scientific-term."
        self.assertEqual(norm.normalize(protected, kind="paragraph", trusted_lines=True)[0], protected)
        for kind in ("list", "table", "unknown"):
            self.assertEqual(norm.normalize(raw, kind=kind, trusted_lines=True)[0], raw)
        self.assertEqual(norm.normalize("Short verse\nAnother verse", kind="paragraph")[0], "Short verse\nAnother verse")
        self.assertEqual(norm.normalize("12.5 3/4\n100 200", kind="paragraph", trusted_lines=True)[0], "12.5 3/4\n100 200")

    def test_three_page_continuation_preserves_identity_and_reverses_exactly(self):
        blocks = [block("a", 44, "The middle, insulated with silk,", geometry=True),
                  block("b", 45, "remains neutral and the charge", geometry=True),
                  block("c", 46, "continues until ready.", geometry=True)]
        before = copy.deepcopy(blocks)
        evidence = norm.project(blocks, [b["text"] for b in blocks])
        self.assertEqual(blocks[0]["text"], "The middle, insulated with silk, remains neutral and the charge continues until ready.")
        self.assertEqual([b["text"] for b in blocks[1:]], ["", ""])
        self.assertEqual(blocks[0]["provenance"]["source_pages"], [44,45,46])
        self.assertEqual(blocks[0]["provenance"]["continuation_block_ids"], ["b", "c"])
        self.assertEqual(blocks[2]["provenance"]["text_consumed_by"], "a")
        final = [b["text"] for b in blocks]
        norm.project(blocks, [b["text"] for b in before])
        self.assertEqual([b["text"] for b in blocks], final)
        by_id = {b["block_id"]: b for b in blocks}
        for change in reversed(evidence["transforms"]):
            by_id[change["block_id"]]["text"] = change["before"]
            if "continuation_block_id" in change:
                by_id[change["continuation_block_id"]]["text"] = change["continuation_before"]
        self.assertEqual([b["text"] for b in before], [b["text"] for b in blocks])

    def test_no_join_across_omissions_columns_indentation_or_structural_blocks(self):
        for change in ("gap", "missing_geometry", "indent", "short_line", "narrow_column", "sentence_end", "heading", "figure", "table"):
            with self.subTest(change=change):
                blocks = [block("a",120,"cannot be taken into consideration", geometry=True), block("b",121,"in detail in the description", geometry=True)]
                if change == "gap": blocks[1]["provenance"]["source_page"] = 124
                elif change == "missing_geometry": blocks[0]["provenance"].pop("line_geometry")
                elif change == "indent": blocks[1]["provenance"]["line_geometry"]["first_line"][0] = 130
                elif change == "short_line": blocks[0]["provenance"]["line_geometry"]["last_line"][2] = 500
                elif change == "narrow_column": blocks[0]["provenance"]["line_geometry"]["bbox"][2] = 350
                elif change == "sentence_end": blocks[0]["text"] += "."
                else: blocks.insert(1, block("separator",120,"",kind=change))
                before = [b["text"] for b in blocks]
                norm.project(blocks, before)
                self.assertEqual([b["text"] for b in blocks], before)


class SelectionAndProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.book = {"pages_filter": "44,119-120,124", "page_selection": {"source_total": 534, "requested_pages": [44,119,120,124]},
                     "pages": [{"page": n, "text": text, "layout_blocks": [{"label": "Text", "text": text, "reading_order": 1}]}
                               for n,text in [(44,"work biomagnetical-\nly, the rod will grow"), (119,"never be obliged to do that"), (120,"cannot be taken into consideration"), (124,"a fluid condenser")]]}
        self.save()
        (self.root/"review_priority.json").write_text(json.dumps({"schema_version":"1.0", "artifact_type":"booktext.review_priority", "items":[], "counts":{}}))

    def tearDown(self): self.temp.cleanup()

    def save(self): (self.root/"book.json").write_text(json.dumps(self.book))

    def test_missing_duplicate_out_of_order_or_unrequested_page_fails_closed(self):
        original = copy.deepcopy(self.book)
        for mode in ("missing", "duplicate", "order", "unexpected"):
            with self.subTest(mode=mode):
                self.book = copy.deepcopy(original)
                if mode == "missing": self.book["pages"].pop(2)
                elif mode == "duplicate": self.book["pages"].append(self.book["pages"][-1])
                elif mode == "order": self.book["pages"].reverse()
                else: self.book["pages"].append({"page":125, "text":"unexpected"})
                self.save()
                with self.assertRaises(ValueError): cdm.build_document(self.root)

    def test_legacy_include_exclude_and_unknown_total_are_not_fabricated(self):
        self.book.pop("page_selection")
        self.book["pages_filter"] = "44,119-120,123-124"; self.book["excluded_pages"] = "123"
        selected = continuity.selection(self.root, self.book)
        self.assertEqual(selected["requested_pages"], [44,119,120,124])
        self.assertEqual(selected["completeness"], "verified")
        self.assertIn("44, 119–120, 124", continuity.selection_notice(selected))
        self.book.pop("pages_filter"); self.book.pop("excluded_pages")
        selected = continuity.selection(self.root, self.book)
        self.assertEqual(selected["completeness"], "unverifiable_legacy_selection")
        self.assertIsNone(selected["requested_pages"])

    def test_saved_native_indentation_rebuilds_paragraphs_without_editing_raw(self):
        lines = []
        for i,(x,text) in enumerate([(100,"The first paragraph has a long physical line"), (100,"and continues here without a terminal point"),
                                   (108,"An indented new paragraph begins here"), (100,"and continues on the next physical line.")]):
            box = [x,50+14*i,400,60+14*i]
            lines.append({"bbox":box,"text":text,"words":[{"text":text,"bbox":box}]})
        text = "\n".join(line["text"] for line in lines)
        self.book = {"page_selection":{"source_total":1,"requested_pages":[1]},"pages":[{"page":1,"text":text,
                     "source_provenance":{"page_size_points":[500,700]},
                     "layout_blocks":[{"label":"Text","reading_order":1,"extraction":"native_pdf_words","bbox":[100,50,400,102],"lines":lines,"text":text}]}]}
        self.save(); before = (self.root/"book.json").read_bytes()
        document = cdm.build_document(self.root)
        paragraphs = [b for b in document["blocks"] if b["type"] == "paragraph"]
        self.assertEqual(len(paragraphs), 2)
        self.assertTrue(paragraphs[1]["text"].startswith("An indented"))
        self.assertNotIn("\n", paragraphs[0]["text"])
        self.assertEqual(" ".join(b["text"] for b in paragraphs), text.replace("\n"," "))
        self.assertEqual(before, (self.root/"book.json").read_bytes())
        self.assertEqual(document, cdm.build_document(self.root))
        self.assertIsNone(document["metadata"]["selection_notice"])

    def test_conflicting_selection_and_broken_continuation_provenance_fail_closed(self):
        self.book["extraction_policy"] = "automatic-extraction-1"
        (self.root/"extraction").mkdir()
        (self.root/"extraction/source.json").write_text(json.dumps({"selected_pages": [44,119]}))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            continuity.selection(self.root, self.book)
        self.book.pop("extraction_policy")
        document = cdm.build_document(self.root)
        paragraphs = [b for b in document["blocks"] if b["type"] == "paragraph"]
        paragraphs[0]["provenance"].update(continuation_block_ids=[paragraphs[1]["block_id"]], source_pages=[44,45])
        paragraphs[1]["provenance"]["text_consumed_by"] = paragraphs[0]["block_id"]
        paragraphs[1]["text"] = ""
        with self.assertRaisesRegex(ValueError, "continuation"):
            cdm.validate_document(document)

    def test_all_reader_formats_preserve_excerpt_boundaries(self):
        document = cdm.build_document(self.root); cdm.write_document(document,self.root)
        manifest = exports.export_document(self.root, ["markdown","html","docx","epub","pdf"])
        self.assertEqual(set(manifest["generated"]), {"markdown","html","docx","epub","pdf"})
        for kind in ("markdown", "html"):
            path = self.root/"exports"/("book.md" if kind == "markdown" else "book.html")
            self.assertEqual(path.read_text().count("Excerpt break"), 2)
        with zipfile.ZipFile(self.root/"exports/book.docx") as package:
            xml = ET.fromstring(package.read("word/document.xml"))
            text = "".join(node.text or "" for node in xml.iter() if node.tag.endswith("}t"))
            self.assertEqual(text.count("Excerpt break"), 2)
            self.assertEqual(text.count("never be obliged"), 1)
            self.assertEqual(len(xml.findall(".//"+exports.W+"br")), 0)
        with zipfile.ZipFile(self.root/"exports/book.epub") as package:
            text = " ".join(package.read(name).decode() for name in package.namelist() if name.startswith("EPUB/text/") and name.endswith(".xhtml"))
            self.assertEqual(text.count("Excerpt break"), 2)
        result = subprocess.run(["pdftotext", str(self.root/"exports/book.pdf"), "-"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.count("Excerpt break"), 2)
        self.assertEqual(result.stdout.count("a fluid condenser"), 1)

    def test_current_cdm_cli_offline_deterministic_raw_unchanged_and_preview_warnings(self):
        before = (self.root/"book.json").read_bytes()
        repo = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(repo/"booktext.py"), "machine", "cdm", str(self.root)]
        result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "PATH":""})
        self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["event_type"], "job_completed")
        document = json.loads((self.root/"book_document.json").read_text())
        self.assertEqual(document, cdm.build_document(self.root))
        self.assertEqual(before, (self.root/"book.json").read_bytes())
        self.assertEqual(len(document["selection"]["gaps"]), 2)
        preview = document_preview(self.root)
        self.assertTrue(any(b.get("normalization_warnings") for b in preview["blocks"]))
        self.assertEqual(sum(b.get("boundary_kind") == "excerpt_gap" for b in preview["blocks"]), 2)
        for render in (exports.render_markdown, exports.render_html):
            text = render(document, self.root)
            self.assertEqual(text.count("Excerpt break"), 2)
            self.assertIn("45–118", text); self.assertIn("121–123", text)
            for fragment in ("never be obliged", "a fluid condenser", "biomagnetical-ly"):
                self.assertEqual(text.count(fragment), 1)
        broken = copy.deepcopy(document)
        broken["blocks"] = [b for b in broken["blocks"] if b.get("boundary_kind") != "excerpt_gap"]
        with self.assertRaisesRegex(ValueError, "boundary"): cdm.validate_document(broken)


if __name__ == "__main__": unittest.main()
