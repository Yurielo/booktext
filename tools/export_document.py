#!/usr/bin/env python3
"""Publication exports from BookText CDM only; never from raw OCR."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from tools.owned_workspace import OwnedWorkspace, is_supervised, supervise
    from tools.canonical_document import validate_document
    from tools import document_identity, translation
except ModuleNotFoundError:  # helper may be invoked directly by another Python
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.owned_workspace import OwnedWorkspace, is_supervised, supervise
    from tools.canonical_document import validate_document
    from tools import document_identity, translation


EXPORT_SCHEMA = "1.0"
# ``pdf_image`` is deliberately separate from the publication PDF.  It is a
# rasterized version of the CDM-derived publication layout, so text cannot be
# selected or copied from it.  It is not a source-page facsimile.
FORMATS = ("markdown", "html", "docx", "epub", "pdf", "pdf_image")
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _load(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def _sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _write(path, value): Path(path).write_text(value, encoding="utf-8")
def _asset_path(value): return f"../{value}" if value else ""


def _grid(root, block):
    path = root / block["artifacts"]["json"]
    data = _load(path)
    grid = data.get("grid")
    if not isinstance(grid, list): raise ValueError("structured table artifact lacks grid")
    return [[str(cell) for cell in row] for row in grid]


def _markdown_table(grid):
    if not grid: return ""
    width = max(len(row) for row in grid)
    rows = [row + [""] * (width - len(row)) for row in grid]
    safe = lambda cell: cell.replace("|", "\\|").replace("\n", " ")
    header = "| " + " | ".join(safe(cell) for cell in rows[0]) + " |"
    divider = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(safe(cell) for cell in row) + " |" for row in rows[1:]]
    return "\n".join([header, divider, *body])


def _markdown_text(value):
    """Keep source punctuation from accidentally becoming Markdown syntax."""
    text = str(value or "")
    # A source footnote marker such as ``* With regard`` is prose, not a
    # list.  Escaping only a leading marker leaves authentic interior text
    # intact and avoids changing CDM text/provenance.
    if re.match(r"^\s*(?:[*+-]|\d+[.)])\s+", text):
        prefix = re.match(r"^(\s*)(.*)$", text)
        text = prefix.group(1) + "\\" + prefix.group(2)
    return text


def render_markdown(document, root, asset_map=None):
    asset_map = asset_map or {}
    lines = [f"# {document['metadata'].get('title') or 'BookText export'}", "", "<!-- Generated from BookText CDM; CDM remains the machine source. -->"]
    if document["metadata"].get("selection_notice"):
        lines.extend(["", "*" + document["metadata"]["selection_notice"] + "*"])
    for block in document["blocks"]:
        kind, provenance = block["type"], block.get("provenance", {})
        page = provenance.get("source_page")
        if kind == "page_break":
            # Provenance remains in CDM; a source-page boundary is not a
            # publication rule and must not draw a line in Word/Pandoc output.
            lines.extend(["", f"<!-- source-page: {page} -->"])
            if block.get("boundary_kind") == "excerpt_gap":
                lines.extend(["", "*" + block["text"] + "*"])
        elif kind == "heading": lines.extend(["", "## " + _markdown_text(block.get("text", ""))])
        elif kind == "paragraph": lines.extend(["", _markdown_text(block.get("text", ""))])
        elif kind == "list": lines.extend(["", "- " + _markdown_text(block.get("text", ""))])
        # Unknown is a conservative CDM classification, not reader-facing
        # content. Keep its text without a developer diagnostic label.
        elif kind == "unknown": lines.extend(["", _markdown_text(block.get("text", ""))])
        elif kind == "table":
            lines.extend([""])
            if block.get("table_status") == "structured": lines.append(_markdown_table(_grid(root, block)))
            elif block.get("fallback_visual"):
                # The source crop preserves a real table without claiming
                # unverified native geometry is a grid of machine cells.
                lines.append(f"![]({asset_map.get(block['fallback_visual'], _asset_path(block['fallback_visual']))})")
            else: continue
        elif kind == "figure":
            lines.extend(["", f"![]({asset_map.get(block.get('image'), _asset_path(block.get('image')))})"])
            if block.get("caption_id"): lines.append(block["caption_id"])
            elif block.get("description"): lines.append(f"\n*Description (auxiliary): {block['description']}*")
    return "\n".join(lines).rstrip() + "\n"


def render_html(document, root, asset_map=None):
    asset_map = asset_map or {}
    parts = ["<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\"><title>BookText export</title>", "<style>body{font:18px system-ui;max-width:900px;margin:2em auto;line-height:1.5}table{border-collapse:collapse}td,th{border:1px solid #999;padding:.3em}.unknown,.fallback{border-left:4px solid #b87900;padding:.6em;background:#fff8e8}img{max-width:100%}</style><body>"]
    if document["metadata"].get("selection_notice"):
        parts.append("<p><em>" + html.escape(document["metadata"]["selection_notice"]) + "</em></p>")
    for block in document["blocks"]:
        kind, provenance = block["type"], block.get("provenance", {}); page = provenance.get("source_page")
        text = html.escape(str(block.get("text", ""))).replace("\n", "<br>\n")
        if kind == "page_break":
            parts.append(f"<!-- source-page: {page} -->")
            if block.get("boundary_kind") == "excerpt_gap":
                parts.append(f'<p class="excerpt-gap"><em>{text}</em></p>')
        elif kind == "heading": parts.append(f"<h2 data-source-page=\"{page}\">{text}</h2>")
        elif kind == "paragraph": parts.append(f"<p data-source-page=\"{page}\">{text}</p>")
        elif kind == "list": parts.append(f"<ul data-source-page=\"{page}\"><li>{text}</li></ul>")
        elif kind == "unknown": parts.append(f"<p data-source-page=\"{page}\">{text}</p>")
        elif kind == "table":
            if block.get("table_status") == "fallback_review":
                if block.get("fallback_visual"):
                    image = html.escape(asset_map.get(block["fallback_visual"], _asset_path(block["fallback_visual"])), quote=True)
                    parts.append(f'<figure data-source-page="{page}"><img src="{image}" alt="source table"></figure>')
                continue
            else:
                grid = _grid(root, block); rows = []
                for row_index, row in enumerate(grid):
                    tag = "th" if row_index == 0 else "td"; rows.append("<tr>" + "".join(f"<{tag}>{html.escape(cell)}</{tag}>" for cell in row) + "</tr>")
                parts.append(f"<table data-source-page=\"{page}\">" + "".join(rows) + "</table>")
        elif kind == "figure":
            parts.append(f"<figure data-source-page=\"{page}\"><img src=\"{html.escape(asset_map.get(block.get('image'), _asset_path(block.get('image'))), quote=True)}\" alt=\"figure\">")
            if block.get("caption_id"): parts.append(f"<figcaption>{html.escape(block['caption_id'])}</figcaption>")
            elif block.get("description"): parts.append(f"<figcaption>Auxiliary description: {html.escape(block['description'])}</figcaption>")
            parts.append("</figure>")
    return "".join(parts) + "</body></html>\n"


def _pandoc(markdown, destination, root):
    executable = shutil.which("pandoc")
    if not executable: raise ValueError("pandoc is required for DOCX and EPUB export")
    resources = os.pathsep.join((str(markdown.parent), str(root)))
    subprocess.run([executable, str(markdown), "--resource-path", resources, "-o", str(destination)], check=True, capture_output=True, text=True)


def _enable_docx_text_wrapping(destination, *, inline_image_hashes=()):
    """Wrap small figures; keep large illustrations in the paginated flow.

    Pandoc creates valid inline drawings, but Word lays those out as text
    characters.  This post-processing affects only image layout in the DOCX;
    figure identity and source provenance remain solely in the CDM.
    """
    with zipfile.ZipFile(destination, "r") as archive:
        files = {entry.filename: archive.read(entry.filename) for entry in archive.infolist()}
    xml = files.get("word/document.xml")
    if xml is None:
        raise ValueError("DOCX has no word/document.xml")
    root = ET.fromstring(xml)
    relationships = {}
    relationship_xml = files.get("word/_rels/document.xml.rels")
    if relationship_xml:
        for relationship in ET.fromstring(relationship_xml):
            identifier, target = relationship.get("Id"), relationship.get("Target")
            if identifier and target:
                relationships[identifier] = (Path("word") / target).as_posix()
    inline_image_hashes = set(inline_image_hashes)
    parents = {child: parent for parent in root.iter() for child in parent}
    section = root.find(f".//{W}sectPr")
    size = section.find(f"{W}pgSz") if section is not None else None
    margins = section.find(f"{W}pgMar") if section is not None else None
    def twips(node, key, default):
        return int(node.get(f"{W}{key}", default)) if node is not None else default
    # Twips -> EMU. Reserve a little height for the image paragraph itself.
    width = (twips(size, "w", 12240) - twips(margins, "left", 1440) - twips(margins, "right", 1440)) * 635
    height = (twips(size, "h", 15840) - twips(margins, "top", 1440) - twips(margins, "bottom", 1440) - 240) * 635
    converted = 0
    for inline in root.findall(f".//{WP}inline"):
        extent = inline.find(f"{WP}extent")
        if extent is None:
            raise ValueError("DOCX drawing has no size")
        cx, cy = int(extent.get("cx")), int(extent.get("cy"))
        if min(cx, cy, width, height) <= 0:
            raise ValueError("DOCX drawing/page has invalid size")
        blip = inline.find(f".//{A}blip")
        embedded = blip.get(f"{R}embed") if blip is not None else None
        asset = files.get(relationships.get(embedded, ""), b"")
        if asset and hashlib.sha256(asset).hexdigest() in inline_image_hashes:
            # A source-preserved fallback table is already one visual block.
            # Floating it would reflow adjacent paragraphs around an
            # unverified crop and make the reader output misleading.
            continue
        scale = min(1, width / cx, height / cy)
        extent.set("cx", str(round(cx * scale))); extent.set("cy", str(round(cy * scale)))
        for transform in inline.findall(f".//{A}xfrm/{A}ext"):
            transform.set("cx", extent.get("cx")); transform.set("cy", extent.get("cy"))
        if cx > width * .45 or cy > height * .5:
            # Floating book-sized illustrations can overlap one another or
            # run beyond the page. Word paginates inline illustrations safely.
            # Clear any preceding small float before this drawing.
            drawing = parents[inline]
            run = parents[drawing]
            run.insert(list(run).index(drawing), ET.Element(f"{W}br", {f"{W}type": "textWrapping", f"{W}clear": "all"}))
            converted += 1
            continue
        inline.tag = f"{WP}anchor"
        inline.attrib.update({"distT": "0", "distB": "0", "distL": "114300", "distR": "114300", "simplePos": "0", "relativeHeight": "251658240", "behindDoc": "0", "locked": "0", "layoutInCell": "1", "allowOverlap": "0"})
        simple = ET.Element(f"{WP}simplePos", {"x": "0", "y": "0"})
        horizontal = ET.Element(f"{WP}positionH", {"relativeFrom": "column"})
        ET.SubElement(horizontal, f"{WP}posOffset").text = "0"
        vertical = ET.Element(f"{WP}positionV", {"relativeFrom": "paragraph"})
        ET.SubElement(vertical, f"{WP}posOffset").text = "0"
        inline.insert(0, simple); inline.insert(1, horizontal); inline.insert(2, vertical)
        # ``extent`` and optional ``effectExtent`` precede wrap in the Word
        # schema.  Inserting after them makes this valid for Word/LibreOffice.
        wrap_index = 3
        while wrap_index < len(inline) and inline[wrap_index].tag in {f"{WP}extent", f"{WP}effectExtent"}:
            wrap_index += 1
        inline.insert(wrap_index, ET.Element(f"{WP}wrapSquare", {"wrapText": "bothSides"}))
        converted += 1
    if not converted:
        return 0
    files["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    temporary = destination.with_suffix(".wrapping.tmp.docx")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    temporary.replace(destination)
    return converted


def _format_wide_docx_tables(destination):
    """Presentation only: give prose more space than short numeric fields.

    Never change cell text/order or infer missing/merged cells. Complex tables
    remain untouched. This pass is deterministic and operates on final OOXML.
    """
    with zipfile.ZipFile(destination) as archive:
        files = {entry.filename: archive.read(entry.filename) for entry in archive.infolist()}
    root = ET.fromstring(files["word/document.xml"])
    section = root.find(f".//{W}sectPr")
    size = section.find(f"{W}pgSz") if section is not None else None
    margins = section.find(f"{W}pgMar") if section is not None else None
    def number(node, key, default):
        return int(node.get(f"{W}{key}", default)) if node is not None else default
    available = number(size, "w", 12240) - number(margins, "left", 1440) - number(margins, "right", 1440)
    if available <= 0:
        raise ValueError("DOCX has invalid printable width")
    def property(parent, tag, **attributes):
        node = parent.find(f"{W}{tag}")
        if node is None:
            node = ET.Element(f"{W}{tag}")
            before = {
                "keepNext": {"keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr", "pBdr", "shd", "tabs", "spacing", "ind", "jc"},
                "tblW": {"jc", "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook"},
                "tblBorders": {"shd", "tblLayout", "tblCellMar", "tblLook"},
                "tblLayout": {"tblCellMar", "tblLook"}, "tblCellMar": {"tblLook"},
                "spacing": {"ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection", "textAlignment", "outlineLvl", "rPr", "sectPr"},
                "sz": {"szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang"},
                "szCs": {"highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang"},
                "tcW": {"gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark"},
            }.get(tag, set())
            position = 0 if tag.endswith("Pr") else next(
                (index for index, child in enumerate(parent) if child.tag.removeprefix(W) in before), len(parent))
            parent.insert(position, node)
        node.attrib.update({f"{W}{key}": str(value) for key, value in attributes.items()})
        return node
    changed = 0
    for table in root.findall(f".//{W}tbl"):
        grid = table.find(f"{W}tblGrid")
        columns = list(grid) if grid is not None else []
        rows = [row.findall(f"{W}tc") for row in table.findall(f"{W}tr")]
        if (len(columns) < 8 or not rows or any(len(row) != len(columns) for row in rows)
                or table.find(f".//{W}gridSpan") is not None
                or table.find(f".//{W}vMerge") is not None
                or table.find(f".//{W}tbl") is not None):
            continue
        weights = []
        for index in range(len(columns)):
            # OOXML t nodes only: formatting whitespace is not cell content.
            texts = [" ".join(node.text or "" for node in row[index].iter(f"{W}t")) for row in rows]
            weights.append(max(6, min(36, max((len(text) for text in texts), default=0) + 2)))
        widths = [available * weight // sum(weights) for weight in weights]
        widths[-1] += available - sum(widths)
        props = property(table, "tblPr")
        property(props, "tblW", w=available, type="dxa")
        property(props, "tblLayout", type="fixed")
        padding = property(props, "tblCellMar")
        for side in ("left", "right", "top", "bottom"):
            property(padding, side, w=40 if side in {"left", "right"} else 50, type="dxa")
        borders = property(props, "tblBorders")
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            property(borders, side, val="single", sz=4, color="D9D9D9")
        for column, width in zip(columns, widths):
            column.set(f"{W}w", str(width))
        for row_index, row in enumerate(rows):
            for cell, width in zip(row, widths):
                cell_props = property(cell, "tcPr")
                property(cell_props, "tcW", w=width, type="dxa")
                property(cell_props, "vAlign", val="center")
                for paragraph in cell.findall(f"{W}p"):
                    paragraph_props = property(paragraph, "pPr")
                    property(paragraph_props, "spacing", before=0, after=0, line=240, lineRule="auto")
                    # Avoid an isolated one/two-row table start at a page foot.
                    # This is pagination, not an inferred semantic header.
                    if row_index < min(2, len(rows)-1):
                        property(paragraph_props, "keepNext", val=1)
                for run in cell.iter(f"{W}r"):
                    run_props = property(run, "rPr")
                    property(run_props, "sz", val=18)
                    property(run_props, "szCs", val=18)
        changed += 1
    if changed:
        files["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        temporary = destination.with_suffix(".tables.tmp.docx")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        temporary.replace(destination)
    return changed


def _format_native_geometry_docx_tables(destination, *, grids=()):
    """Keep verified native-PDF mappings as text tables, not invented headers.

    Pandoc's Markdown table syntax necessarily treats its first row as a
    header.  Native PDF geometry does not supply that semantic evidence, so
    remove the resulting header treatment and grid decoration from precisely
    the matching source-derived tables in the final DOCX.
    """
    expected = {tuple(tuple(str(cell) for cell in row) for row in grid) for grid in grids}
    if not expected:
        return 0
    with zipfile.ZipFile(destination) as archive:
        files = {entry.filename: archive.read(entry.filename) for entry in archive.infolist()}
    root = ET.fromstring(files["word/document.xml"])
    changed = 0
    for table in root.findall(f".//{W}tbl"):
        rows = []
        for row in table.findall(f"{W}tr"):
            rows.append(tuple("".join(node.text or "" for node in cell.iter(f"{W}t")) for cell in row.findall(f"{W}tc")))
        if tuple(rows) not in expected:
            continue
        properties = table.find(f"{W}tblPr")
        if properties is None:
            properties = ET.Element(f"{W}tblPr")
            table.insert(0, properties)
        look = properties.find(f"{W}tblLook")
        if look is None:
            look = ET.SubElement(properties, f"{W}tblLook")
        look.attrib.update({f"{W}firstRow": "0", f"{W}lastRow": "0", f"{W}noHBand": "1", f"{W}noVBand": "1"})
        borders = properties.find(f"{W}tblBorders")
        if borders is None:
            borders = ET.SubElement(properties, f"{W}tblBorders")
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            edge = borders.find(f"{W}{side}")
            if edge is None:
                edge = ET.SubElement(borders, f"{W}{side}")
            edge.attrib.clear(); edge.set(f"{W}val", "nil")
        for row in table.findall(f"{W}tr"):
            row_properties = row.find(f"{W}trPr")
            if row_properties is not None:
                for header in list(row_properties.findall(f"{W}tblHeader")):
                    row_properties.remove(header)
            for run_properties in row.findall(f".//{W}rPr"):
                for bold in list(run_properties.findall(f"{W}b")) + list(run_properties.findall(f"{W}bCs")):
                    run_properties.remove(bold)
        changed += 1
    if changed:
        files["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        temporary = destination.with_suffix(".native-tables.tmp.docx")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        temporary.replace(destination)
    return changed


def _renderer_python():
    candidates = [os.environ.get("BOOKTEXT_EXPORT_PYTHON"), sys.executable, str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3")]
    for candidate in candidates:
        if not candidate: continue
        check = subprocess.run([candidate, "-c", "import reportlab"], capture_output=True)
        if check.returncode == 0: return candidate
    raise ValueError("publication PDF requires Python with reportlab; set BOOKTEXT_EXPORT_PYTHON")


def _render_pdf(document_path, destination):
    result = subprocess.run([_renderer_python(), str(Path(__file__).resolve()), "_render_pdf", str(document_path), str(destination)], capture_output=True, text=True)
    if result.returncode:
        raise ValueError("publication PDF rendering failed: " + (result.stderr or result.stdout).strip())


def _render_image_pdf(publication_pdf, destination):
    """Create a no-text-layer PDF from the publication PDF's rendered pages."""
    renderer = shutil.which("pdftoppm")
    if renderer is None:
        raise ValueError("image-only PDF requires pdftoppm (Poppler)")
    with OwnedWorkspace(prefix="booktext-pdf-image-") as temporary:
        prefix = Path(temporary) / "page"
        result = subprocess.run(
            [renderer, "-png", "-r", "150", str(publication_pdf), str(prefix)],
            capture_output=True,
            text=True,
        )
        pages = sorted(Path(temporary).glob("page-*.png"))
        if result.returncode or not pages:
            detail = (result.stderr or result.stdout).strip()
            raise ValueError("image-only PDF page rendering failed" + (": " + detail if detail else ""))
        result = subprocess.run(
            [_renderer_python(), str(Path(__file__).resolve()), "_render_image_pdf", str(destination), *(str(page) for page in pages)],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise ValueError("image-only PDF assembly failed: " + (result.stderr or result.stdout).strip())


def _validate(path, fmt, root):
    if not path.is_file() or path.stat().st_size == 0: raise ValueError(f"missing or empty {fmt} export")
    if fmt in {"docx", "epub"}:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "word/document.xml"} if fmt == "docx" else {"mimetype", "META-INF/container.xml", "EPUB/content.opf", "EPUB/nav.xhtml"}
            if bad or not required.issubset(names): raise ValueError(f"invalid {fmt} archive")
    elif fmt in {"pdf", "pdf_image"}:
        tool = shutil.which("pdfinfo")
        if tool and subprocess.run([tool, str(path)], capture_output=True).returncode != 0: raise ValueError("unreadable PDF")
    else:
        content = path.read_text(encoding="utf-8")
        if not content.strip(): raise ValueError(f"empty {fmt}")
        if fmt == "markdown":
            for asset in re.findall(r"!\[[^]]*\]\(([^)]+)\)", content):
                if not (path.parent / asset).resolve().is_file(): raise ValueError("broken Markdown asset reference")
        if fmt == "html":
            for asset in re.findall(r'(?:src|href)="([^"]+)"', content):
                if asset.startswith(("http:", "https:", "#")): raise ValueError("remote HTML asset is not allowed")
                if asset and not (path.parent / asset).resolve().is_file(): raise ValueError("broken HTML asset reference")
    return {"valid": True, "bytes": path.stat().st_size, "sha256": _sha(path)}


def export_document(output_dir, formats, *, document_override=None, export_relative="exports", basename="book", provenance=None, deliverable_label_prefix="publication"):
    """Export canonical CDM, or a separately validated derivative projection.

    ``document_override`` is intentionally in-memory.  Translation uses this
    path so it cannot rewrite the canonical CDM or raw OCR artifacts.
    """
    root = Path(output_dir).expanduser().resolve(); document_path = root / "book_document.json"
    document = document_override if document_override is not None else _load(document_path); decisions_path = root / "review_decisions.json"
    decisions = _load(decisions_path) if decisions_path.is_file() else None
    validate_document(document, root=root, decisions=decisions)
    selected = tuple(dict.fromkeys(formats or ()))
    if not selected:
        raise ValueError("select at least one export format; use 'all' explicitly for every format")
    requested = FORMATS if "all" in selected else selected
    if any(value not in FORMATS for value in requested): raise ValueError("unsupported export format")
    export_dir = root / export_relative; export_dir.mkdir(parents=True, exist_ok=True)
    asset_map = {}
    needs_local_assets = any(fmt in requested for fmt in ("markdown", "html", "docx", "epub"))
    if needs_local_assets:
        for block in document["blocks"]:
            image = block.get("image") if block["type"] == "figure" else block.get("fallback_visual") if block["type"] == "table" else None
            if not image: continue
            source = root / image
            if not source.is_file(): raise ValueError("CDM figure asset is missing")
            destination = export_dir / "assets" / f"{block['block_id']}{source.suffix.lower()}"
            destination.parent.mkdir(exist_ok=True); shutil.copyfile(source, destination)
            asset_map[image] = str(destination.relative_to(export_dir))
    markdown, html_text = render_markdown(document, root, asset_map), render_html(document, root, asset_map)
    paths = {"markdown": export_dir / f"{basename}.md", "html": export_dir / f"{basename}.html", "docx": export_dir / f"{basename}.docx", "epub": export_dir / f"{basename}.epub", "pdf": export_dir / f"{basename}.pdf", "pdf_image": export_dir / f"{basename}-image.pdf"}
    if "markdown" in requested: _write(paths["markdown"], markdown)
    if "html" in requested: _write(paths["html"], html_text)
    has_figures = any(block.get("type") == "figure" or bool(block.get("fallback_visual")) for block in document["blocks"])
    if "docx" in requested:
        docx_markdown = export_dir / f".{basename}-docx-input.md"
        _write(docx_markdown, markdown)
        try:
            _pandoc(docx_markdown, paths["docx"], root)
            fallback_hashes = {_sha(root / block["fallback_visual"]) for block in document["blocks"]
                               if block.get("type") == "table" and block.get("fallback_visual")}
            _enable_docx_text_wrapping(paths["docx"], inline_image_hashes=fallback_hashes)
            _format_wide_docx_tables(paths["docx"])
            native_grids = [_grid(root, block) for block in document["blocks"]
                            if block.get("type") == "table" and block.get("table_status") == "structured"
                            and (block.get("provenance", {}).get("source") or {}).get("kind") == "native_pdf_words_geometry"]
            _format_native_geometry_docx_tables(paths["docx"], grids=native_grids)
        finally:
            docx_markdown.unlink(missing_ok=True)
    if "epub" in requested:
        epub_markdown = export_dir / f".{basename}-epub-input.md"
        _write(epub_markdown, markdown)
        try: _pandoc(epub_markdown, paths["epub"], root)
        finally: epub_markdown.unlink(missing_ok=True)
    temporary_document = None
    if document_override is not None and any(fmt in requested for fmt in ("pdf", "pdf_image")):
        # Keep the temporary projection at the BookText root: PDF rendering
        # resolves CDM figure/table references relative to that root.
        temporary_document = root / f".{basename}-translation-export-document.json"
        _write(temporary_document, json.dumps(document, ensure_ascii=False))
        render_document_path = temporary_document
    else:
        render_document_path = document_path
    if "pdf" in requested: _render_pdf(render_document_path, paths["pdf"])
    if "pdf_image" in requested:
        source_pdf = paths["pdf"]
        temporary_source = None
        if "pdf" not in requested:
            temporary_source = export_dir / ".book-image-source.pdf"
            source_pdf = temporary_source
            _render_pdf(render_document_path, source_pdf)
        try: _render_image_pdf(source_pdf, paths["pdf_image"])
        finally:
            if temporary_source: temporary_source.unlink(missing_ok=True)
    if temporary_document: temporary_document.unlink(missing_ok=True)
    validations = {fmt: _validate(paths[fmt], fmt, root) for fmt in requested}
    identity_sources = document.get("metadata", {}).get("source_files") or [document.get("metadata", {}).get("title") or "BookText document"]
    named = {
        fmt: str(document_identity.register_deliverable(root, paths[fmt].relative_to(root), f"{deliverable_label_prefix} {fmt.upper()}", sources=identity_sources))
        for fmt in requested
    }
    block_counts = {kind: sum(block["type"] == kind for block in document["blocks"]) for kind in ("unknown", "table")}
    visual_mode = "classified_or_recovered_visual_crops" if has_figures else "no_visual_evidence"
    warnings = list(document.get("warnings", []))
    fallback_blocks = [block for block in document["blocks"] if block["type"] == "table" and block.get("table_status") == "fallback_review"]
    for block in fallback_blocks:
        page = block.get("provenance", {}).get("source_page")
        if block.get("fallback_visual"):
            warnings.append({"kind": "fallback_table_source_visual_preserved", "page": page, "message": "No verified table cells: source table visual retained without a fabricated grid."})
        else:
            warnings.append({"kind": "fallback_table_not_rendered", "page": page, "message": "No verified table cells: retained in CDM/review evidence, not rendered as publication text."})
    if visual_mode == "no_visual_evidence":
        warnings.append({"kind": "no_visual_evidence_for_docx", "message": "CDM has no figure blocks or retained source-page assets; DOCX cannot safely add images."})
    manifest = {"schema_version": EXPORT_SCHEMA, "artifact_type": "booktext.export_manifest", "cdm": {"path": "book_document.json", "sha256": _sha(document_path), "schema_version": document["schema_version"]}, "booktext_version": document["metadata"].get("booktext_version"), "generated": {fmt: {"path": str(paths[fmt].relative_to(root)), "named_deliverable": named[fmt], **validations[fmt]} for fmt in requested}, "validation": {"cdm": "PASS", "formats": {fmt: "PASS" for fmt in requested}}, "warnings": warnings, "visual_preservation": {"mode": visual_mode, "figure_count": sum(block.get("type") == "figure" for block in document["blocks"]), "fallback_table_visual_count": sum(bool(block.get("fallback_visual")) for block in fallback_blocks), "policy": "Only CDM figure blocks and explicit fallback-table source crops are embedded. Prepared source pages are provenance, never whole-page figure fallbacks."}, "unresolved_priority_counts": document.get("review", {}).get("unresolved_priority_counts", {}), "fallback_unknown_counts": {"unknown": block_counts["unknown"], "fallback_tables": sum(block.get("table_status") == "fallback_review" for block in document["blocks"] if block["type"] == "table")}, "policy": "Publication outputs are derived from CDM only. CDM remains the canonical machine source for Integra Evidence ingestion."}
    if provenance:
        manifest.update(provenance)
    _write(export_dir / "export_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def export_translation(output_dir, formats, *, source_language, target_language, history_sha=None):
    """Export an existing validated derivative translation without model calls."""
    root = Path(output_dir).expanduser().resolve()
    pair = f"{source_language}-{target_language}"
    if history_sha is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", history_sha):
            raise ValueError("history SHA must be a 64-character lowercase hex digest")
        manifest_path = root / "translations" / "history" / f"{pair}-{history_sha}.json"
        translation_manifest = _load(manifest_path)
        if not translation_manifest or translation._digest(translation_manifest) != history_sha:
            raise ValueError("historical translation is missing or has invalid digest")
        source_document = _load(root / "book_document.json")
        validation = translation._validate_manifest(translation_manifest, source_document, cdm_sha=translation.sha_file(root / "book_document.json"))
    else:
        validation = translation.validate_translation(root, source_language=source_language, target_language=target_language)
        manifest_path = translation.translation_path(root, source_language, target_language)
        translation_manifest = _load(manifest_path)
    if not validation["valid"] or validation["stale"]:
        raise ValueError("translation must be valid and source-matched before export")
    source_document = _load(root / "book_document.json")
    document = json.loads(json.dumps(source_document, ensure_ascii=False))
    entries = {entry["block_id"]: entry for entry in translation_manifest["blocks"]}
    for block in document["blocks"]:
        entry = entries.get(block.get("block_id"))
        if entry and isinstance(entry.get("translation"), str):
            block["text"] = entry["translation"]
    title = document.get("metadata", {}).get("title") or "BookText export"
    document.setdefault("metadata", {})["title"] = f"{title} ({pair} translation)"
    suffix = f"/history/{history_sha}" if history_sha else ""
    return export_document(root, formats, document_override=document, export_relative=f"translations/exports/{pair}{suffix}", basename=f"book-{pair}", deliverable_label_prefix=f"translation {pair}",
        provenance={"artifact_type": "booktext.translation_export_manifest", "translation": {"path": str(manifest_path.relative_to(root)), "translation_id": translation_manifest["translation_id"], "validation": validation, "raw_ocr_mutated": False}})


def _pdf_main(document_path, destination):
    """Executed with the local ReportLab-enabled Python; receives CDM only."""
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.utils import ImageReader
    from PIL import Image as PILImage
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
    document = _load(document_path); root = Path(document_path).parent
    font = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
    if Path(font).is_file(): pdfmetrics.registerFont(TTFont("BookTextUnicode", font))
    styles = getSampleStyleSheet()
    for style in styles.byName.values(): style.fontName = "BookTextUnicode" if Path(font).is_file() else "Helvetica"
    story = []
    if document["metadata"].get("selection_notice"):
        story.extend([Paragraph(html.escape(document["metadata"]["selection_notice"]), styles["Italic"]), Spacer(1, 12)])
    for block in document["blocks"]:
        kind = block["type"]
        if kind == "page_break":
            if block.get("boundary_kind") == "excerpt_gap":
                story.extend([Spacer(1, 12), Paragraph(html.escape(block["text"]), styles["Italic"]), Spacer(1, 12)])
            # Physical source page boundaries do not break the reflowed book.
        elif kind == "heading": story.extend([Paragraph(block.get("text", ""), styles["Heading2"]), Spacer(1, 8)])
        elif kind in {"paragraph", "list", "unknown"}:
            prefix = "• " if kind == "list" else ""
            story.extend([Paragraph(prefix + html.escape(block.get("text", "")).replace("\n", "<br/>"), styles["BodyText"]), Spacer(1, 5)])
        elif kind == "table":
            if block.get("table_status") == "fallback_review":
                asset = root / block.get("fallback_visual", "")
                if asset.is_file():
                    image = Image(str(asset)); image._restrictSize(650, 450); story.extend([image, Spacer(1, 8)])
                continue
            else:
                table = Table(_grid(root, block), repeatRows=1)
                table.setStyle(TableStyle([("GRID", (0,0), (-1,-1), .25, colors.grey), ("FONTNAME", (0,0), (-1,-1), "BookTextUnicode" if Path(font).is_file() else "Helvetica"), ("FONTSIZE", (0,0), (-1,-1), 7), ("BACKGROUND", (0,0), (-1,0), colors.lightgrey)])); story.extend([table, Spacer(1, 8)])
        elif kind == "figure":
            asset = root / block.get("image", "")
            if asset.is_file():
                try:
                    with PILImage.open(asset) as candidate: candidate.load()
                    ImageReader(str(asset)).getSize()
                    image = Image(str(asset)); image._restrictSize(650, 450); story.append(image)
                except Exception:
                    story.append(Paragraph("Figure asset retained in CDM but cannot be rendered in this PDF.", styles["Italic"]))
            if block.get("caption_id"): story.append(Paragraph(html.escape(block["caption_id"]), styles["Italic"]))
            elif block.get("description"): story.append(Paragraph("Auxiliary description: " + html.escape(block["description"]), styles["Italic"]))
            story.append(Spacer(1, 8))
    SimpleDocTemplate(str(destination), pagesize=landscape(letter), title=document["metadata"].get("title") or "BookText export").build(story)


def _image_pdf_main(destination, page_images):
    """Assemble rendered PNG pages only: intentionally no selectable text."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image as PILImage
    output = canvas.Canvas(str(destination))
    for page in page_images:
        with PILImage.open(page) as image:
            width, height = image.size
        page_width, page_height = width * 72 / 150, height * 72 / 150
        output.setPageSize((page_width, page_height))
        output.drawImage(ImageReader(str(page)), 0, 0, width=page_width, height=page_height)
        output.showPage()
    output.save()


def main(argv=None):
    if argv and argv[0] == "_render_pdf": _pdf_main(Path(argv[1]), Path(argv[2])); return
    if argv and argv[0] == "_render_image_pdf": _image_pdf_main(Path(argv[1]), [Path(value) for value in argv[2:]]); return
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("output_dir", type=Path); parser.add_argument("--format", action="append", required=True, choices=[*FORMATS, "all"])
    args = parser.parse_args(argv); print(json.dumps(export_document(args.output_dir, args.format), ensure_ascii=False))


if __name__ == "__main__":
    if not is_supervised():
        sys.exit(supervise([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]))
    main(sys.argv[1:])
