"""Automatic PDF routing using saved geometry, never a second OCR engine.

Native words and visual crops remain source evidence. Ambiguous raster regions
are preserved, not silently converted to text. Scan routing needs positive
text-line evidence. The production caller owns OCR, QA and child lifecycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import tempfile
from collections import Counter
import xml.etree.ElementTree as ET
from pathlib import Path

from tools.review_decisions import atomic_write_json

POLICY = "automatic-extraction-2"


def atomic_text(path, text):
    """Do not leave a truncated reusable evidence file after interruption."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def output_directory(source, *, automatic=False, explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    source = Path(source)
    # Never replace a legacy run (and its human decisions/translations).
    return source.parent / f"{source.stem}_book{'_auto' if automatic else ''}"


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def box(node):
    return [float(node.attrib[key]) for key in ("xMin", "yMin", "xMax", "yMax")]


def overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def parse_native(xml):
    root = ET.fromstring(xml)
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    pages = []
    for page in root.iter("page"):
        blocks = []
        for block in page.iter("block"):
            lines = []
            for line in block.iter("line"):
                words = [{"text": word.text or "", "bbox": box(word)} for word in line.iter("word")]
                if words:
                    lines.append({"bbox": box(line), "words": words, "text": " ".join(word["text"] for word in words)})
            if lines:
                blocks.append({"bbox": box(block), "lines": lines})
        pages.append({"width": float(page.attrib["width"]), "height": float(page.attrib["height"]), "blocks": blocks})
    offsets = Counter()
    for number,page in enumerate(pages,1):
        page["running_number_candidates"] = [line["text"] for block in page["blocks"] for line in block["lines"]
            if line["text"].isdecimal() and line["bbox"][1] > page["height"]*.8]
        offsets.update({int(value)-number for value in page["running_number_candidates"]})
    for number,page in enumerate(pages,1):
        page["running_numbers"] = [value for value in page.pop("running_number_candidates") if offsets[int(value)-number] >= 3]
    return pages


def text_quality(blocks):
    words = [word for block in blocks for line in block["lines"] for word in line["words"]]
    text = " ".join(word["text"] for word in words)
    invalid = sum(char == "\ufffd" or (ord(char) < 32 and not char.isspace()) for char in text)
    duplicates = len(words) - len({(w["text"], tuple(w["bbox"])) for w in words})
    return {"usable": bool(text.strip()) and invalid == 0 and duplicates <= len(words) * .1,
            "words": len(words), "invalid_characters": invalid, "duplicate_words": duplicates}


def scan_overlay_evidence(page, trace, regions):
    """Identify invisible native words over a page scan, without guessing quality.

    MuPDF ignore_text (PDF rendering mode 3) is positive provenance evidence.
    A hidden watermark or a hidden layer without a page raster is insufficient.
    Glyph origins are mapped into the same device coordinates as native boxes.
    """
    raster = [r["bbox"] for r in regions if r["kind"] == "raster"
              and area(r["bbox"]) >= page["width"] * page["height"] * .8]
    points = {"hidden": [], "visible": []}
    if not raster:
        return set(), {"route": "native_or_residual", "reason": "no_page_scan"}
    for node in ET.fromstring(trace).iter():
        if node.tag not in {"ignore_text", "fill_text", "stroke_text"}:
            continue
        hidden = node.tag == "ignore_text" or float(node.get("alpha", "1")) == 0
        a,b,c,d,e,f = map(float, node.get("transform", "1 0 0 1 0 0").split())
        for glyph in node.iter("g"):
            if not glyph.get("unicode", "").strip():
                continue
            x,y = float(glyph.get("x", "0")),float(glyph.get("y", "0"))
            points["hidden" if hidden else "visible"].append((a*x+c*y+e,b*x+d*y+f))
    words = [w for block in page["blocks"] for line in block["lines"] for w in line["words"]]
    candidates = set()
    def hits(word, kind):
        x0,y0,x1,y1 = word["bbox"]
        return sum(x0-.25 <= x < x1 and y0-.5 <= y <= y1+.5 for x,y in points[kind])
    for index,word in enumerate(words):
        if not any(overlap(word["bbox"], bounds) >= area(word["bbox"]) * .8 for bounds in raster):
            continue
        if hits(word, "hidden") >= max(1, len(word["text"].strip()) * .5) and hits(word, "visible") == 0:
            candidates.add(index)
    # Require a substantive overlay, not a handful of invisible annotations.
    overlay = candidates if len(candidates) >= 20 else set()
    return overlay, {"route": "scan_with_ocr_overlay" if overlay else "native_or_residual",
                     "reason": "invisible_words_over_page_raster" if overlay else "insufficient_overlay_evidence",
                     "invisible_overlay_words": len(overlay), "native_words": len(words),
                     "scan_regions": raster if overlay else [], "render_dpi": 300 if overlay else 200,
                     "quality_claim": "origin_only; existing OCR is not assumed correct or incorrect"}


def without_overlay_words(page, indices):
    """Project the visible native layer; keep native XML evidence untouched."""
    index = 0
    blocks = []
    for block in page["blocks"]:
        lines = []
        for line in block["lines"]:
            words = []
            for word in line["words"]:
                if index not in indices: words.append(word)
                index += 1
            if words: lines.append({**line, "words": words, "text": " ".join(w["text"] for w in words)})
        if lines: blocks.append({**block, "lines": lines})
    return {**page, "blocks": blocks}


def trace_regions(xml, width, height):
    """MuPDF trace uses device coordinates including rotation/crop transforms."""
    root = ET.fromstring(xml)
    images, paths = [], []
    def transformed(points, element):
        a, b, c, d, e, f = map(float, element.get("transform", "1 0 0 1 0 0").split())
        pts = [(a*x+c*y+e, b*x+d*y+f) for x,y in points]
        return [max(0, min(p[0] for p in pts)), max(0, min(p[1] for p in pts)),
                min(width, max(p[0] for p in pts)), min(height, max(p[1] for p in pts))]
    for node in root.iter():
        if node.tag in {"fill_image", "fill_image_mask"}:
            bounds = transformed([(0,0),(0,1),(1,0),(1,1)], node)
            if area(bounds) > 1:
                images.append({"bbox": bounds, "kind": "raster", "matrix": list(map(float,node.get("transform", "1 0 0 1 0 0").split())), "pixel_width": node.get("width"), "pixel_height": node.get("height")})
        elif node.tag in {"fill_path", "stroke_path"}:
            points = []
            for command in node:
                for x,y in (("x","y"),("x1","y1"),("x2","y2"),("x3","y3")):
                    if x in command.attrib and y in command.attrib:
                        points.append((float(command.get(x)), float(command.get(y))))
            if points:
                bounds = transformed(points, node)
                if 4 < area(bounds) < width*height*.8 and bounds[2]-bounds[0] > 2 and bounds[3]-bounds[1] > 2:
                    paths.append(bounds)
    # Join only touching/nearby vector marks, not distant page-wide furniture.
    groups = []
    for bounds in paths:
        touching = [group for group in groups if overlap([bounds[0]-4,bounds[1]-4,bounds[2]+4,bounds[3]+4], union(group))]
        merged = [bounds]
        for group in touching:
            merged.extend(group); groups.remove(group)
        groups.append(merged)
    for group in groups:
        bounds = union(group)
        if len(group) >= 3 and area(bounds) > 20 and not any(overlap(bounds,i["bbox"]) > area(bounds)*.8 for i in images):
            images.append({"bbox": bounds, "kind": "vector", "path_count": len(group)})
    # Duplicate drawing invocations must not duplicate an image in the book.
    return list({tuple(round(v,2) for v in item["bbox"]): item for item in images}.values())


def embedded_assets(source, number, regions, evidence, run):
    """Use independently decoded XObjects only when occurrence mapping is exact.

    This prevents page text overlapping an image's white margin from becoming
    duplicated inside a display crop. Masked/skewed/clipped/composite images
    retain the render-crop fallback, never a guessed image association.
    """
    from PIL import Image
    raster = [region for region in regions if region["kind"] == "raster"]
    if not raster: return {}
    listing = run(["pdfimages", "-f", str(number), "-l", str(number), "-list", source], capture=True, quiet=True)
    entries = [line.split() for line in listing.stdout.splitlines() if re.match(r"\s*\d+\s+\d+\s",line)]
    if len(entries) != len(raster) or any(entry[2] != "image" for entry in entries): return {}
    prefix = evidence / f"page-{number:04d}-embedded"
    paths = [Path(f"{prefix}-{int(entry[1]):03d}.png") for entry in entries]
    if not all(path.is_file() for path in paths):
        run(["pdfimages", "-png", "-f", str(number), "-l", str(number), source, prefix], capture=True, quiet=True)
    output = {}
    for region,entry,path in zip(raster,entries,paths):
        a,b,c,d,_e,_f = region["matrix"]
        bounds = region["bbox"]
        if abs(abs(a)+abs(c)-(bounds[2]-bounds[0])) > .01 or abs(abs(b)+abs(d)-(bounds[3]-bounds[1])) > .01: continue
        if not path.is_file() or entry[3:5] != [region["pixel_width"], region["pixel_height"]]: continue
        with Image.open(path) as source_image:
            asset = source_image.copy()
        if abs(b) < .001 and abs(c) < .001:
            if a < 0: asset = asset.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if d < 0: asset = asset.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        elif abs(a) < .001 and abs(d) < .001:
            asset = asset.transpose(Image.Transpose.TRANSPOSE)
            if c < 0: asset = asset.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if b < 0: asset = asset.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        else: continue
        output[tuple(bounds)] = (asset, path)
    return output


def scan_line_evidence(image):
    """Positive regular text-row evidence, not an image-only == scan shortcut."""
    from PIL import Image
    gray = image.convert("L")
    gray.thumbnail((800, 1000), Image.Resampling.LANCZOS)
    w,h = gray.size
    data = list(gray.get_flattened_data() if hasattr(gray, "get_flattened_data") else gray.getdata())
    rows = [sum(p < 150 for p in data[y*w:(y+1)*w]) / w for y in range(h)]
    bands = []
    start = None
    for y, value in enumerate(rows + [0]):
        if .015 < value < .55:
            if start is None: start = y
        elif start is not None:
            if 3 <= y-start <= h*.045: bands.append((start,y))
            start = None
    gaps = [b[0]-a[0] for a,b in zip(bands,bands[1:])]
    regular = bool(gaps) and statistics.pstdev(gaps) / max(1,statistics.mean(gaps)) < .45
    return {"text_rows": len(bands), "regular_rows": regular,
            "scan_like": len(bands) >= 10 and regular and sum(rows)/h < .22}


def native_layout(page, exclusions):
    """Word boxes preserve spaces; indent/gap evidence preserves paragraphs."""
    blocks = []
    heights = [line["bbox"][3]-line["bbox"][1] for block in page["blocks"] for line in block["lines"]]
    body_height = statistics.median(heights) if heights else 10
    for raw in page["blocks"]:
        chunks = []
        for line in raw["lines"]:
            words = [w for w in line["words"] if not any(overlap(w["bbox"], b) > area(w["bbox"])*.5 for b in exclusions)]
            if not words: continue
            line = {**line, "words": words, "text": " ".join(w["text"] for w in words)}
            previous = chunks[-1][-1] if chunks else None
            gap = line["bbox"][1] - previous["bbox"][3] if previous else 0
            indent = line["bbox"][0] - raw["bbox"][0]
            # A first-line indent is often smaller than one character height.
            # Requiring terminal punctuation loses real paragraphs after a
            # quotation/comma; requiring >1 height misses common book indents.
            first_line_indent = (previous and indent > body_height*.65
                                 and previous["bbox"][0]-raw["bbox"][0] <= body_height*.5)
            if not previous or gap > body_height or first_line_indent:
                chunks.append([])
            chunks[-1].append(line)
        for lines in chunks:
            bounds = union([line["bbox"] for line in lines])
            text = "\n".join(line["text"] for line in lines)
            label = "Text"
            if len(lines) == 1 and bounds[3]-bounds[1] > body_height*1.3:
                label = "SectionHeader"
            if len(lines) == 1 and text.strip() in page.get("running_numbers", []):
                label = "PageNumber"
            blocks.append({"label": label, "text": text, "bbox": bounds, "lines": lines,
                           "reading_order": len(blocks)+1, "extraction": "native_pdf_words"})
    return blocks


def retain_scanned_figures(output, record, page_result):
    """Retain Surya-labelled figures inside a raster scan, without another OCR.

    The raw inference stays untouched. Figure blocks are excluded from the
    publication text and Vision QA input by the caller. No guessed cells or
    generated description is introduced.
    """
    from PIL import Image, ImageDraw
    output = Path(output)
    raw_ref = "surya/surya-input/results.json"
    visuals = []
    width,height = record["source_provenance"]["page_size_points"]
    with Image.open(record["image"]) as full:
        full.load()
        ib = page_result.get("image_bbox") or [0,0,full.width,full.height]
        if ib[2] <= ib[0] or ib[3] <= ib[1]: raise ValueError("invalid Surya image coordinates")
        for index,block in enumerate(page_result.get("blocks", []), 1):
            if str(block.get("label", "")).casefold() != "figure": continue
            bounds = block.get("bbox")
            if not isinstance(bounds, list) or len(bounds) != 4:
                raise ValueError("Surya Figure lacks trustworthy coordinates")
            normalized = [(value-ib[i%2]) / (ib[i%2+2]-ib[i%2]) for i,value in enumerate(bounds)]
            normalized = [max(0,min(1,value)) for value in normalized]
            points = [value * (width if i%2==0 else height) for i,value in enumerate(normalized)]
            if area(points) <= 0: raise ValueError("empty Surya Figure coordinates")
            if any(overlap(points,visual["bbox"]) > area(points)*.9 for visual in record["visuals"]): continue
            pixels = [round(value*(full.width if i%2==0 else full.height)) for i,value in enumerate(normalized)]
            image = output / "images" / f"page-{record['page']:04d}-layout-figure-{index:02d}.png"
            image.parent.mkdir(exist_ok=True)
            crop = full.crop(pixels)
            if not image.exists(): crop.save(image, dpi=(crop.width*72/(points[2]-points[0]),crop.height*72/(points[3]-points[1])))
            visuals.append({"index": index, "type":"figure", "image":str(image), "bbox":points, "ocr_action":"exclude",
                "source_provenance":{"kind":"surya_layout_source_crop", "source_page":record["page"],
                    "source_sha256":record["source_provenance"]["source_sha256"], "raw_layout":raw_ref,
                    "layout_index":index, "bbox_points":points, "sha256":sha(image),
                    "translation_performed":False, "policy":"preserved from primary layout evidence; no figure text promoted"}})
    if visuals:
        with Image.open(record["ocr_page"]) as source_mask:
            masked = source_mask.convert("RGB")
        draw = ImageDraw.Draw(masked)
        for visual in visuals:
            draw.rectangle([round(v*(masked.width/width if i%2==0 else masked.height/height)) for i,v in enumerate(visual["bbox"])],fill="white")
        qa_path = output / ".work" / f"page-{record['page']:04d}-qa.png"
        qa_path.parent.mkdir(exist_ok=True); masked.save(qa_path)
        record["ocr_page"] = qa_path
        record["visuals"].extend(visuals)
        checksums_path = output / "extraction/artifacts.json"
        checksums = json.loads(checksums_path.read_text())
        for visual in visuals: checksums[str(Path(visual["image"]).relative_to(output))] = sha(visual["image"])
        atomic_write_json(checksums_path, checksums)
    return visuals


def prepare_pdf(source, output, selected, run, *, event=None, options=None):
    """Prepare evidence and masks. Never call an OCR/LLM here."""
    from PIL import Image, ImageDraw
    output = Path(output)
    evidence = output / "extraction"
    evidence.mkdir(parents=True, exist_ok=True)
    fingerprint = sha(source)
    anchor = {"policy": POLICY, "source_sha256": fingerprint, "selected_pages": selected, "options": options or {}}
    anchor_path = evidence / "source.json"
    if anchor_path.exists() and json.loads(anchor_path.read_text()) != anchor:
        raise RuntimeError("stale extraction workspace: source/options changed; choose a new --output-dir")
    if (output / "book.json").exists() and not anchor_path.exists():
        raise RuntimeError("refusing to overwrite legacy OCR/review artifacts; choose a new --output-dir")
    checksums_path = evidence / "artifacts.json"
    checksums = json.loads(checksums_path.read_text()) if checksums_path.exists() else {}
    for relative,digest in checksums.items():
        artifact = (output / relative).resolve()
        if not artifact.is_relative_to(output.resolve()) or not artifact.is_file() or sha(artifact) != digest:
            raise RuntimeError(f"stale extraction artifact: {relative}; original evidence has changed")
    atomic_write_json(anchor_path, anchor)
    xml_path = evidence / "native.xml"
    if not xml_path.exists():
        result = run(["pdftotext", "-cropbox", "-bbox-layout", source, "-"], capture=True, quiet=True)
        atomic_text(xml_path, result.stdout)
    native_pages = parse_native(xml_path.read_text())
    atomic_write_json(evidence / "inventory.json", {"schema_version": "1.0", "source_sha256": fingerprint,
        "source_page_count": len(native_pages), "pages": [{"page": number, **text_quality(page["blocks"])} for number,page in enumerate(native_pages,1)]})
    prepared = {}
    for ordinal, number in enumerate(selected, 1):
        if event: event({"phase": "prepare", "current": ordinal, "total": len(selected), "pages": [number]})
        page = native_pages[number-1]
        width,height = page["width"],page["height"]
        image_path = output / "pages" / f"page-{number:04d}.png"
        image_path.parent.mkdir(exist_ok=True)
        trace_path = evidence / f"page-{number:04d}-trace.xml"
        if not trace_path.exists():
            trace = run(["mutool", "draw", "-q", "-F", "trace", source, str(number)], capture=True, quiet=True)
            atomic_text(trace_path, trace.stdout)
        trace_xml = trace_path.read_text()
        regions = trace_regions(trace_xml, width, height)
        overlay_words, routing = scan_overlay_evidence(page, trace_xml, regions)
        visible_page = without_overlay_words(page, overlay_words)
        render_diagnostics = ""
        if not image_path.exists():
            rendered = run(["pdftoppm", "-cropbox", "-f", str(number), "-l", str(number), "-r", str(routing.get("render_dpi", 200)), "-singlefile", "-png", source, image_path.with_suffix("")], capture=True, quiet=True)
            render_diagnostics = (rendered.stderr or "").strip()
            atomic_text(evidence / f"page-{number:04d}-render.txt", render_diagnostics)
        elif (evidence / f"page-{number:04d}-render.txt").exists():
            render_diagnostics = (evidence / f"page-{number:04d}-render.txt").read_text()
        quality = text_quality(page["blocks"])
        visible_quality = text_quality(visible_page["blocks"])
        originals = embedded_assets(source, number, regions, evidence, run)
        visuals, scans, warnings = [], [], []
        if render_diagnostics:
            warnings.append({"page": number, "kind": "source_render_diagnostic", "message": render_diagnostics})
        with Image.open(image_path) as full:
            full.load()
            sx,sy = full.width/width,full.height/height
            def pixel_box(bounds): return tuple(round(v * (sx if i%2 == 0 else sy)) for i,v in enumerate(bounds))
            for index, region in enumerate(regions, 1):
                bounds = region["bbox"]
                if overlay_words and bounds in routing["scan_regions"]:
                    scans.append(bounds); continue
                crop = full.crop(pixel_box(bounds))
                probe = crop
                native_words = [word for block in page["blocks"] for line in block["lines"] for word in line["words"]
                                if overlap(bounds,word["bbox"]) > area(word["bbox"])*.8]
                # Partial text layers must not hide the remaining scan. Probe
                # the residual after masking *words*, not a page-wide paragraph.
                if region["kind"] == "raster" and area(bounds) > width*height*.8 and quality["usable"] and native_words:
                    probe = crop.convert("RGB"); ink = ImageDraw.Draw(probe)
                    for word in native_words:
                        x0,y0,x1,y1 = word["bbox"]
                        ink.rectangle(((x0-bounds[0])*sx-2,(y0-bounds[1])*sy-2,(x1-bounds[0])*sx+2,(y1-bounds[1])*sy+2), fill="white")
                    original_ink = sum(crop.convert("L").histogram()[:150])
                    remaining_ink = sum(probe.convert("L").histogram()[:150])
                    if remaining_ink < max(20, original_ink*.03): continue
                rows = scan_line_evidence(probe) if region["kind"] == "raster" else {"scan_like": False}
                if rows["scan_like"]:
                    scans.append(bounds); continue
                path = output / "images" / f"page-{number:04d}-visual-{index:02d}.png"
                path.parent.mkdir(exist_ok=True)
                original = originals.get(tuple(bounds))
                source_asset = original[1] if original else None
                if original:
                    crop = original[0]
                if not path.exists(): crop.save(path, dpi=(crop.width*72/(bounds[2]-bounds[0]), crop.height*72/(bounds[3]-bounds[1])))
                visuals.append({"index": index, "type": region["kind"], "image": str(path), "bbox": bounds,
                    "ocr_action": "exclude", "source_provenance": {"kind": "pdf_embedded_image" if original else "pdf_display_crop", "source_sha256": fingerprint,
                    "source_page": number, "bbox_points": bounds, "page_size_points": [width,height], "trace": str(trace_path.relative_to(output)),
                    "original_asset": str(source_asset.relative_to(output)) if source_asset else None,
                    "original_sha256": sha(source_asset) if source_asset else None, "display_matrix": region.get("matrix"),
                    "sha256": sha(path), "ocr_performed": False, "translation_performed": False}})
                if region["kind"] == "raster" and area(bounds) > width*height*.2:
                    warnings.append({"page": number, "kind": "ambiguous_raster_preserved", "message": "Raster preserved without OCR; inspect the source if it contains body text."})
                if region["kind"] == "vector":
                    warnings.append({"page": number, "kind": "vector_region_preserved", "message": "Vector region retained as a source crop; semantic structure and any table cells remain unverified."})
            excluded = [v["bbox"] for v in visuals]
            # Only words truly inside a figure are excluded; slight overlaps
            # at an image's blank edge still belong to the native text layer.
            layout = native_layout(visible_page, excluded) if visible_quality["usable"] else []
            needs_ocr = bool(scans) or (not quality["usable"] and (quality["words"] > 0 or not visuals))
            if needs_ocr:
                masked = full.convert("RGB")
                draw = ImageDraw.Draw(masked)
                for bounds in excluded + [word["bbox"] for b in layout for line in b["lines"] for word in line["words"]]:
                    x0,y0,x1,y1 = pixel_box(bounds)
                    draw.rectangle((x0-2,y0-2,x1+2,y1+2), fill="white")
                ocr_path = output / ".work" / f"page-{number:04d}-auto.png"
                ocr_path.parent.mkdir(exist_ok=True)
                masked.save(ocr_path)
            else: ocr_path = image_path
        text = "\n\n".join(b["text"] for b in layout)
        record = {"page": number, "image": image_path, "ocr_page": ocr_path, "needs_ocr": needs_ocr,
                  "text": text, "layout_blocks": layout, "visuals": visuals, "warnings": warnings,
                  "source_provenance": {"kind": "mixed" if needs_ocr and layout else "surya" if needs_ocr else "native_pdf",
                  "source_sha256": fingerprint, "source_page": number, "quality": quality,
                  "routing": routing, "native_evidence": "extraction/native.xml",
                  "trace_evidence": str(trace_path.relative_to(output)), "page_size_points": [width,height]}}
        prepared[number] = record
        for artifact in [xml_path, trace_path, image_path, *[Path(visual["image"]) for visual in visuals], *[original[1] for original in originals.values()]]:
            checksums[str(artifact.relative_to(output))] = sha(artifact)
        atomic_write_json(checksums_path, checksums)
    atomic_write_json(evidence / "report.json", {**anchor, "pages": [{k:v for k,v in record.items() if k not in {"image","ocr_page","visuals","text","layout_blocks"}} for record in prepared.values()]})
    return prepared, len(native_pages)
