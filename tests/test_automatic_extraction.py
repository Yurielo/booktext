import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
import booktext
from tools import automatic_extraction as auto, canonical_document as cdm, text_normalization as norm, translation
from tools.machine_protocol import processing_preflight

ROOT = Path(__file__).resolve().parents[1]


def pdf_fixture(path, scan=False, hidden_overlay=False):
    # Real PDF syntax through the real production CLI, no model/test converter.
    stream = b"BT /F1 16 Tf 50 740 Td (DOCUMENT TITLE) Tj /F1 10 Tf 0 -40 Td (This paragraph contains ordinary printed words and a visible illustration.) Tj 0 -14 Td (The second physical line must flow within the same paragraph.) Tj ET 80 480 100 50 re S 85 485 90 40 re S 90 490 80 30 re S"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream"]
    if scan:
        image=Image.new("RGB",(600,800),"white"); draw=ImageDraw.Draw(image)
        for y in range(70,700,35):
            for x in range(70,470,30): draw.rectangle((x,y,x+10,y+9),fill="black")
        raster=zlib.compress(image.tobytes()); stream=b"q 600 0 0 800 0 0 cm /Im1 Do Q"
        if hidden_overlay:
            stream += b" BT /F1 10 Tf 3 Tr 70 721 Td (" + b"old "*30 + b") Tj ET"
        objects[2]=b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> /XObject << /Im1 6 0 R >> >> /Contents 5 0 R >>"
        objects[4]=b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream"
        objects.append(b"<< /Type /XObject /Subtype /Image /Width 600 /Height 800 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length "+str(len(raster)).encode()+b" >>\nstream\n"+raster+b"\nendstream")
    data = b"%PDF-1.4\n"; offsets = [0]
    for number,obj in enumerate(objects,1):
        offsets.append(len(data)); data += f"{number} 0 obj\n".encode()+obj+b"\nendobj\n"
    xref = len(data); data += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]: data += f"{offset:010d} 00000 n \n".encode()
    data += f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(data)


class AutomaticExtractionTests(unittest.TestCase):
    def test_overlay_requires_hidden_words_over_a_scan_and_preserves_visible_text(self):
        words = [{"text":"old", "bbox":[10+i*10,10,19+i*10,20]} for i in range(25)]
        words.append({"text":"Native", "bbox":[10,30,50,40]})
        page = {"width":300,"height":400,"blocks":[{"bbox":[10,10,290,40],"lines":[{"bbox":[10,10,290,40],"words":words}]}]}
        hidden = ''.join(f'<g unicode="o" x="{11+i*10}" y="19"/><g unicode="l" x="{14+i*10}" y="19"/>' for i in range(25))
        trace = f'<page><ignore_text>{hidden}</ignore_text><fill_text><g unicode="N" x="11" y="39"/></fill_text></page>'
        regions = [{"kind":"raster","bbox":[0,0,300,400]}]
        indices, evidence = auto.scan_overlay_evidence(page,trace,regions)
        self.assertEqual(set(range(25)),indices)
        self.assertEqual("scan_with_ocr_overlay",evidence["route"])
        projected = auto.without_overlay_words(page,indices)
        self.assertEqual(["Native"],[w["text"] for b in projected["blocks"] for l in b["lines"] for w in l["words"]])
        self.assertEqual(26,len(page["blocks"][0]["lines"][0]["words"]))
        self.assertEqual(set(), auto.scan_overlay_evidence(page,trace,[])[0])
        self.assertEqual(set(), auto.scan_overlay_evidence(page,trace.replace('ignore_text','fill_text'),regions)[0])
        self.assertEqual(set(), auto.scan_overlay_evidence(page,'<page><ignore_text><g unicode="o" x="11" y="19"/></ignore_text></page>',regions)[0])
        # Device-coordinate origin matching also handles rotated/translated
        # traces and invisible alpha without changing the native source.
        transformed = ''.join(f'<g unicode="o" x="19" y="{-11-i*10}"/><g unicode="l" x="19" y="{-14-i*10}"/>' for i in range(25))
        rotated = f'<page><ignore_text transform="0 1 -1 0 0 0">{transformed}</ignore_text></page>'
        self.assertEqual(set(range(25)), auto.scan_overlay_evidence(page,rotated,regions)[0])
        self.assertEqual(indices, auto.scan_overlay_evidence(page,trace.replace('<ignore_text>', '<fill_text alpha="0">').replace('</ignore_text>', '</fill_text>'),regions)[0])

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("pdftotext","pdfimages","pdftoppm","mutool")), "Poppler/MuPDF integration")
    def test_hidden_ocr_over_scan_goes_through_production_primary_ocr_once(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/"old-ocr.pdf"; pdf_fixture(source,scan=True,hidden_overlay=True)
            output=Path(folder)/"result"
            def recognize(pages,destination,*args,**kwargs):
                self.assertEqual(len(pages),1)
                with Image.open(pages[0][1]) as scan:
                    self.assertLess(scan.convert("L").getpixel((round(75*scan.width/600),round(75*scan.height/800))),100)
                raw=destination/"surya-input/results.json"; raw.parent.mkdir(parents=True,exist_ok=True)
                raw.write_text(json.dumps({"page-0001":[{"page":1,"image_bbox":[0,0,600,800],"blocks":[{"label":"Text","html":"<p>Fresh primary text.</p>","bbox":[70,70,470,100],"reading_order":1}]}]}))
                return {1:"Fresh primary text."}
            with patch.object(sys,"argv",["booktext",str(source),"--no-qa","--no-pdf","--output-dir",str(output)]), patch.object(booktext,"require_surya"), patch.object(booktext,"recognize_pages_surya",side_effect=recognize) as ocr, contextlib.redirect_stdout(io.StringIO()):
                booktext.main()
            ocr.assert_called_once()
            book=json.loads((output/"book.json").read_text())
            self.assertEqual("Fresh primary text.",book["pages"][0]["text"])
            self.assertEqual([],book["pages"][0]["visuals"])
            report=json.loads((output/"extraction/report.json").read_text())
            self.assertEqual("scan_with_ocr_overlay",report["pages"][0]["source_provenance"]["routing"]["route"])
            self.assertIn("old",(output/"extraction/native.xml").read_text())
            self.assertNotIn("old",' '.join(b.get("text","") for b in cdm.build_document(output)["blocks"]))

    def test_source_aware_preflight_does_not_require_ocr_for_native_analysis(self):
        with patch("tools.machine_protocol._dependency", side_effect=lambda name, **kw: {"name": name, "available": name in {"pdftotext","pdfimages","pdftoppm","pdfinfo","mutool"}}):
            result = processing_preflight(["book.pdf"], ["--no-pdf"])
        self.assertFalse(result["missing"])
        self.assertEqual(result["ocr"], "auto")

    def test_output_is_distinct_from_legacy_and_explicit_override_is_resolved(self):
        self.assertEqual(auto.output_directory(Path("/tmp/book.pdf"), automatic=True), Path("/tmp/book_book_auto"))
        self.assertNotEqual(auto.output_directory(Path("/tmp/book.pdf"), automatic=True), auto.output_directory(Path("/tmp/book.pdf")))

    def test_native_word_geometry_preserves_spaces_order_and_excludes_figure_letters(self):
        xml = '<doc><page width="200" height="300"><block xMin="0" yMin="0" xMax="180" yMax="100"><line xMin="1" yMin="1" xMax="100" yMax="10"><word xMin="1" yMin="1" xMax="3" yMax="10">a</word><word xMin="7" yMin="1" xMax="60" yMax="10">contract</word></line><line xMin="1" yMin="60" xMax="100" yMax="70"><word xMin="1" yMin="60" xMax="40" yMax="70">FIGURE</word></line></block></page></doc>'
        page = auto.parse_native(xml)[0]
        blocks = auto.native_layout(page, [[0,50,110,80]])
        self.assertEqual([block["text"] for block in blocks], ["a contract"])
        self.assertTrue(auto.text_quality(page["blocks"])["usable"])
        page["blocks"][0]["lines"][0]["words"][0]["text"] = "\ufffd"
        self.assertFalse(auto.text_quality(page["blocks"])["usable"])

    def test_trace_rotation_and_occurrence_identity(self):
        regions = auto.trace_regions('<document><page><fill_image transform="0 20 -30 0 100 40"/><fill_image transform="0 20 -30 0 100 140"/></page></document>', 200,300)
        self.assertEqual([r["bbox"] for r in regions], [[70,40,100,60],[70,140,100,160]])

    def test_embedded_asset_rotation_does_not_include_overlapping_page_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            image=Image.new("RGB",(4,3),"white"); image.putpixel((0,0),(255,0,0))
            image.save(root/"page-0001-embedded-000.png")
            region={"kind":"raster","bbox":[70,40,100,80],"matrix":[0,40,-30,0,100,40],"pixel_width":"4","pixel_height":"3"}
            def run(*args,**kwargs): return type("Result",(),{"stdout":"1 0 image 4 3 rgb 3 8 image no 2 0","stderr":""})()
            assets=auto.embedded_assets(root/"source.pdf",1,[region],root,run)
            display,_=assets[(70,40,100,80)]
            self.assertEqual(display.size,(3,4))
            self.assertEqual(display.getpixel((2,0)),(255,0,0))

    def test_owned_command_timeout_always_cleans_child(self):
        from unittest.mock import Mock
        child=Mock(); child.process.communicate.side_effect=subprocess.TimeoutExpired(["tool"],1)
        with patch.object(booktext.OWNED_CHILDREN,"spawn",return_value=child):
            with self.assertRaises(subprocess.TimeoutExpired): booktext.run(["tool"],timeout=1,quiet=True)
        child.cleanup.assert_called_once()

    def test_scanned_figure_is_retained_from_layout_and_masked_for_qa(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/"extraction").mkdir()
            (root/"extraction/artifacts.json").write_text("{}")
            source = root/"scan.png"; Image.new("RGB",(600,800),"red").save(source)
            record = {"page":1,"image":source,"ocr_page":source,"visuals":[],
                      "source_provenance":{"page_size_points":[300,400],"source_sha256":"source"}}
            raw = {"image_bbox":[0,0,600,800],"blocks":[{"label":"Figure","bbox":[100,200,300,400],"html":"<p>DO NOT TRANSLATE</p>"}]}
            visuals = auto.retain_scanned_figures(root, record, raw)
            self.assertEqual(visuals[0]["bbox"],[50,100,150,200])
            with Image.open(visuals[0]["image"]) as crop: self.assertEqual(crop.size,(200,200))
            with Image.open(record["ocr_page"]) as mask:
                self.assertEqual(mask.getpixel((150,250)),(255,255,255))
                self.assertEqual(mask.getpixel((50,50)),(255,0,0))
            self.assertEqual(auto.retain_scanned_figures(root,record,raw),[])
            self.assertEqual(raw["blocks"][0]["html"],"<p>DO NOT TRANSLATE</p>")

    def test_scan_evidence_requires_many_regular_text_rows_not_solid_picture(self):
        image = Image.new("L", (600,800), 255); draw = ImageDraw.Draw(image)
        for y in range(70,700,35):
            for x in range(70,470,30): draw.rectangle((x,y,x+10,y+9), fill=0)
        self.assertTrue(auto.scan_line_evidence(image)["scan_like"])
        self.assertFalse(auto.scan_line_evidence(Image.new("L", (600,800), 0))["scan_like"])

    def test_figure_framing_restores_touching_pixels_without_caption_or_raw_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/"images").mkdir()
            image = Image.new("RGB", (600,800), "white"); draw = ImageDraw.Draw(image)
            draw.rectangle((95,195,305,405), fill="black")
            draw.rectangle((100,180,300,184), fill="black")  # Separate caption outside six-point range.
            image.save(root/"page.png"); image.crop((100,200,300,400)).save(root/"images/raw.png")
            before = (root/"images/raw.png").read_bytes()
            visual = {"index":1,"bbox":[50,100,150,200],"image":"images/raw.png",
                      "source_provenance":{"kind":"surya_layout_source_crop","raw_layout":"surya/results.json"}}
            framed, warnings = auto.refine_scanned_figure(root, visual, "page.png", [300,400])
            self.assertEqual(warnings, [])
            self.assertEqual(framed["bbox"], visual["bbox"])
            self.assertEqual((root/"images/raw.png").read_bytes(), before)
            self.assertEqual(visual["image"], "images/raw.png")
            box = [round(v*2) for v in framed["source_provenance"]["crop_bbox_points"]]
            self.assertEqual(box, [94,194,307,407])
            with Image.open(root/framed["image"]) as crop:
                self.assertEqual(crop.tobytes(), image.crop(box).tobytes())
            again, _ = auto.refine_scanned_figure(root, visual, "page.png", [300,400])
            self.assertEqual(again, framed)
            Image.new("RGB", (1,1), "black").save(root/framed["image"])
            with self.assertRaisesRegex(ValueError, "stale refined figure"):
                auto.refine_scanned_figure(root, visual, "page.png", [300,400])

    def test_figure_framing_keeps_uncertain_or_nonprimary_edges(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/"images").mkdir()
            Image.new("RGB", (600,800), "black").save(root/"page.png")
            Image.new("RGB", (200,200), "black").save(root/"images/raw.png")
            visual = {"index":1,"bbox":[50,100,150,200],"image":"images/raw.png",
                      "source_provenance":{"kind":"surya_layout_source_crop"}}
            framed, warnings = auto.refine_scanned_figure(root, visual, "page.png", [300,400])
            self.assertEqual(framed, visual); self.assertEqual(len(warnings[0]["edges"]), 4)
            visual["source_provenance"]["kind"] = "native_figure"
            self.assertEqual(auto.refine_scanned_figure(root,visual,"page.png",[300,400]), (visual, []))

    def test_mixed_partial_text_layer_routes_residual_scan_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root/"mixed.pdf"; source.write_bytes(b"pdf")
            output = root/"result"; output.mkdir()
            def run(argv, **kwargs):
                if argv[0] == "pdftotext":
                    text = '<doc><page width="600" height="800"><block xMin="70" yMin="30" xMax="200" yMax="40"><line xMin="70" yMin="30" xMax="200" yMax="40"><word xMin="70" yMin="30" xMax="200" yMax="40">Native</word></line></block></page></doc>'
                elif argv[0] == "pdfimages":
                    text = ""
                elif argv[0] == "mutool":
                    text = '<document><page><fill_image transform="600 0 0 800 0 0"/></page></document>'
                else:
                    image = Image.new("RGB", (600,800), "white"); draw = ImageDraw.Draw(image)
                    draw.rectangle((70,30,200,40), fill="black")
                    for y in range(100,700,35):
                        for x in range(70,470,30): draw.rectangle((x,y,x+10,y+9), fill="black")
                    image.save(str(argv[-1])+".png"); text = ""
                return type("Result",(),{"stdout":text,"stderr":""})()
            pages,_ = auto.prepare_pdf(source, output, [1], run)
            page = pages[1]
            self.assertTrue(page["needs_ocr"])
            self.assertEqual(page["text"], "Native")
            self.assertEqual(page["visuals"], [])
            with Image.open(page["ocr_page"]) as masked:
                self.assertEqual(masked.getpixel((80,35)), (255,255,255))
                self.assertEqual(masked.getpixel((80,105)), (0,0,0))
            # Reuse is source- and artifact-anchored, not filename-based.
            (output/"extraction/native.xml").write_text("<doc/>")
            with self.assertRaisesRegex(RuntimeError, "stale extraction artifact"):
                auto.prepare_pdf(source, output, [1], run)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("pdftotext","pdftoppm","mutool","pdfinfo")), "Poppler/MuPDF integration")
    def test_real_cli_native_pdf_without_ocr_and_deterministic_cdm(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"Document with spaces.pdf"; pdf_fixture(source)
            output = Path(folder)/"output"
            args = [str(source), "--no-pdf", "--output-dir", str(output)]
            with patch.object(sys, "argv", ["booktext", *args]), patch.object(booktext, "require_surya", side_effect=AssertionError("native must not OCR")), patch.object(booktext, "recognize_pages_surya", side_effect=AssertionError("native must not OCR")), contextlib.redirect_stdout(io.StringIO()):
                booktext.main()
            raw = (output/"book.json").read_bytes()
            doc = cdm.build_document(output)
            self.assertEqual(doc, cdm.build_document(output))
            self.assertEqual(raw, (output/"book.json").read_bytes())
            body = next(b for b in doc["blocks"] if b["type"] == "paragraph")
            self.assertNotIn("\n", body["text"])
            self.assertIn("second physical line", body["text"])
            self.assertTrue((output/"extraction/inventory.json").is_file())
            cli = subprocess.run([sys.executable, "booktext.py", "machine", "run", str(source), "--", "--no-pdf", "--output-dir", str(output)], cwd=ROOT, text=True, capture_output=True, timeout=60)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            events = [json.loads(line) for line in cli.stdout.splitlines()]
            self.assertEqual(events[-1]["event_type"], "job_completed")
            self.assertEqual(raw, (output/"book.json").read_bytes())
            source.write_bytes(source.read_bytes()+b"\n% source changed")
            with self.assertRaisesRegex(RuntimeError, "stale"):
                auto.prepare_pdf(source, output, [1], booktext.run)

    def test_auto_ui_has_one_extraction_path(self):
        app = (ROOT/"BookTextApp/Sources/BookTextApp/BookTextApp.swift").read_text()
        self.assertNotIn("useTesseract", app)
        self.assertNotIn("nativeTextOnly", app)
        self.assertIn("AutomaticProcessingOptions.arguments", app)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("pdftotext","pdfimages","pdftoppm","mutool","pdfinfo")), "Poppler/MuPDF integration")
    def test_production_scan_path_uses_primary_ocr_once_and_keeps_layout_figure(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/"scan.pdf"; pdf_fixture(source,scan=True)
            output=Path(folder)/"result"
            def recognize(pages,destination,*args,**kwargs):
                self.assertEqual(len(pages),1)
                raw=destination/"surya-input/results.json"; raw.parent.mkdir(parents=True,exist_ok=True)
                raw.write_text(json.dumps({"page-0001":[{"page":1,"image_bbox":[0,0,600,800],"blocks":[
                    {"label":"Text","html":"<p>Body from primary OCR.</p>","bbox":[70,70,470,100],"reading_order":1},
                    {"label":"Figure","html":"<p>FIGURE LABEL</p>","bbox":[70,300,470,500],"reading_order":2}]}]}))
                return {1:"Body from primary OCR. FIGURE LABEL"}
            with patch.object(sys,"argv",["booktext",str(source),"--no-qa","--no-pdf","--output-dir",str(output)]), patch.object(booktext,"require_surya"), patch.object(booktext,"recognize_pages_surya",side_effect=recognize) as ocr, contextlib.redirect_stdout(io.StringIO()):
                booktext.main()
            ocr.assert_called_once()
            book=json.loads((output/"book.json").read_text())
            self.assertEqual(book["pages"][0]["text"],"Body from primary OCR.")
            self.assertEqual(len(book["pages"][0]["visuals"]),1)
            doc=cdm.build_document(output)
            self.assertEqual([b["type"] for b in doc["blocks"]],["paragraph","figure","page_break"])
            self.assertNotIn("FIGURE LABEL", " ".join(b.get("text","") for b in doc["blocks"]))


class NormalizationTests(unittest.TestCase):
    def test_prose_reflow_is_idempotent_and_numbers_hyphens_survive(self):
        source = "This is a long printed line with 12,5 mg and 3/278 plus a well-\nknown method,which should flow as prose."
        result, reasons = norm.normalize(source, kind="paragraph", trusted_lines=True)
        self.assertEqual(result, "This is a long printed line with 12,5 mg and 3/278 plus a well-known method, which should flow as prose.")
        self.assertEqual(norm.normalize(result, kind="paragraph", trusted_lines=True)[0], result)
        self.assertIn("prose_layout_whitespace", reasons)

    def test_never_guesses_word_splits_or_changes_lists_tables_verse(self):
        for kind,text in [("list","1. one\n2. two"),("unknown","a\tb\n1\t2"),("paragraph","Short verse\nNext line"),("paragraph","• alpha\n• beta")]:
            self.assertEqual(norm.normalize(text, kind=kind)[0], text)
        self.assertEqual(norm.normalize("acontract", kind="paragraph", trusted_lines=True)[0], "acontract")

    def test_attested_cross_page_word_is_owned_once_and_reversible(self):
        blocks = [{"block_id":"a","type":"paragraph","text":"In accor-","provenance":{"source_page":1}}, {"block_id":"b","type":"paragraph","text":"dance with the rule.","provenance":{"source_page":2}}]
        evidence = norm.project(blocks, ["accordance accordance In accor-", "dance with the rule."])
        self.assertEqual(blocks[0]["text"], "In accordance with the rule.")
        self.assertEqual(blocks[1]["text"], "")
        self.assertEqual(blocks[1]["provenance"]["text_consumed_by"], "a")
        self.assertEqual(evidence["transforms"][0]["continuation_before"], "dance with the rule.")

    def test_translation_quality_signals_do_not_modify_text(self):
        source = "These are twelve ordinary words in a paragraph that has not been translated at all."
        signals = translation.quality_signals(source, source, source_language="en", target_language="ru")
        self.assertIn("possible_untranslated_prose", signals)
        self.assertIn("mixed_scripts_inside_word", translation.quality_signals("coherent", "коherentной", source_language="en", target_language="ru"))
        self.assertNotIn("possible_untranslated_prose", translation.quality_signals("Bardon", "Bardon", source_language="en", target_language="ru", kind="heading"))
        self.assertIn("numeric_tokens_changed", translation.quality_signals("3/287", "3/278", source_language="en", target_language="ru"))


if __name__ == "__main__": unittest.main()
