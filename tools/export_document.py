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
from pathlib import Path

try:
    from tools.canonical_document import validate_document
    from tools import document_identity
except ModuleNotFoundError:  # helper may be invoked directly by another Python
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.canonical_document import validate_document
    from tools import document_identity


EXPORT_SCHEMA = "1.0"
FORMATS = ("markdown", "html", "docx", "epub", "pdf")


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


def render_markdown(document, root, asset_map=None, source_page_assets=None):
    asset_map = asset_map or {}
    source_page_assets = source_page_assets or {}
    lines = [f"# {document['metadata'].get('title') or 'BookText export'}", "", "<!-- Generated from BookText CDM; CDM remains the machine source. -->"]
    for block in document["blocks"]:
        kind, provenance = block["type"], block.get("provenance", {})
        page = provenance.get("source_page")
        if kind == "page_break":
            lines.extend(["", f"<!-- source-page: {page} -->", "", "---"])
            if page in source_page_assets:
                # This is a visual facsimile of the whole source page, not a
                # claim that BookText inferred an individual figure/caption.
                lines.extend(["", f"> Source page facsimile, page {page}.", "", f"![source page {page}]({source_page_assets[page]})"])
        elif kind == "heading": lines.extend(["", "## " + block.get("text", "")])
        elif kind == "paragraph": lines.extend(["", block.get("text", "")])
        elif kind == "list": lines.extend(["", "- " + block.get("text", "")])
        elif kind == "unknown": lines.extend(["", f"> **Unknown layout block, source page {page}:**", "> " + block.get("text", "").replace("\n", "\n> ")])
        elif kind == "table":
            lines.extend([""])
            if block.get("table_status") == "structured": lines.append(_markdown_table(_grid(root, block)))
            else: lines.append(f"> **Table fallback review, source page {page}.** No verified cells are available.")
        elif kind == "figure":
            lines.extend(["", f"![figure]({asset_map.get(block.get('image'), _asset_path(block.get('image')))})"])
            if block.get("caption_id"): lines.append(block["caption_id"])
            elif block.get("description"): lines.append(f"\n*Description (auxiliary): {block['description']}*")
    return "\n".join(lines).rstrip() + "\n"


def render_html(document, root, asset_map=None):
    asset_map = asset_map or {}
    parts = ["<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\"><title>BookText export</title>", "<style>body{font:18px system-ui;max-width:900px;margin:2em auto;line-height:1.5}table{border-collapse:collapse}td,th{border:1px solid #999;padding:.3em}.unknown,.fallback{border-left:4px solid #b87900;padding:.6em;background:#fff8e8}img{max-width:100%}</style><body>"]
    for block in document["blocks"]:
        kind, provenance = block["type"], block.get("provenance", {}); page = provenance.get("source_page")
        text = html.escape(str(block.get("text", ""))).replace("\n", "<br>\n")
        if kind == "page_break": parts.append(f"<hr data-source-page=\"{page}\">")
        elif kind == "heading": parts.append(f"<h2 data-source-page=\"{page}\">{text}</h2>")
        elif kind == "paragraph": parts.append(f"<p data-source-page=\"{page}\">{text}</p>")
        elif kind == "list": parts.append(f"<ul data-source-page=\"{page}\"><li>{text}</li></ul>")
        elif kind == "unknown": parts.append(f"<section class=\"unknown\" data-source-page=\"{page}\"><strong>Unknown layout block</strong><br>{text}</section>")
        elif kind == "table":
            if block.get("table_status") == "fallback_review": parts.append(f"<section class=\"fallback\" data-source-page=\"{page}\">Table fallback review; no verified cells are available.</section>")
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


def _validate(path, fmt, root):
    if not path.is_file() or path.stat().st_size == 0: raise ValueError(f"missing or empty {fmt} export")
    if fmt in {"docx", "epub"}:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "word/document.xml"} if fmt == "docx" else {"mimetype", "META-INF/container.xml", "EPUB/content.opf", "EPUB/nav.xhtml"}
            if bad or not required.issubset(names): raise ValueError(f"invalid {fmt} archive")
    elif fmt == "pdf":
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


def export_document(output_dir, formats):
    root = Path(output_dir).expanduser().resolve(); document_path = root / "book_document.json"
    document = _load(document_path); decisions_path = root / "review_decisions.json"
    decisions = _load(decisions_path) if decisions_path.is_file() else None
    validate_document(document, root=root, decisions=decisions)
    requested = FORMATS if "all" in formats else tuple(formats)
    if any(value not in FORMATS for value in requested): raise ValueError("unsupported export format")
    export_dir = root / "exports"; export_dir.mkdir(exist_ok=True)
    asset_map = {}
    for block in document["blocks"]:
        if block["type"] != "figure" or not block.get("image"): continue
        source = root / block["image"]
        if not source.is_file(): raise ValueError("CDM figure asset is missing")
        destination = export_dir / "assets" / f"{block['block_id']}{source.suffix.lower()}"
        destination.parent.mkdir(exist_ok=True); shutil.copyfile(source, destination)
        asset_map[block["image"]] = str(destination.relative_to(export_dir))
    markdown, html_text = render_markdown(document, root, asset_map), render_html(document, root, asset_map)
    paths = {"markdown": export_dir / "book.md", "html": export_dir / "book.html", "docx": export_dir / "book.docx", "epub": export_dir / "book.epub", "pdf": export_dir / "book.pdf"}
    if "markdown" in requested or any(value in requested for value in ("docx", "epub")): _write(paths["markdown"], markdown)
    if "html" in requested: _write(paths["html"], html_text)
    source_page_assets = {}
    has_figures = any(block.get("type") == "figure" for block in document["blocks"])
    if "docx" in requested and not has_figures:
        for source_page in document.get("source_pages", []):
            number, relative = source_page.get("source_page"), source_page.get("prepared_page")
            if not isinstance(number, int) or not isinstance(relative, str):
                continue
            source = root / relative
            if not source.is_file():
                continue
            destination = export_dir / "assets" / f"source-page-{number:04d}{source.suffix.lower()}"
            destination.parent.mkdir(exist_ok=True); shutil.copyfile(source, destination)
            source_page_assets[number] = str(destination.relative_to(export_dir))
    if "docx" in requested:
        docx_markdown = export_dir / ".book-docx-input.md"
        _write(docx_markdown, render_markdown(document, root, asset_map, source_page_assets))
        try:
            _pandoc(docx_markdown, paths["docx"], root)
        finally:
            docx_markdown.unlink(missing_ok=True)
    if "epub" in requested: _pandoc(paths["markdown"], paths["epub"], root)
    if "pdf" in requested: _render_pdf(document_path, paths["pdf"])
    validations = {fmt: _validate(paths[fmt], fmt, root) for fmt in requested}
    identity_sources = document.get("metadata", {}).get("source_files") or [document.get("metadata", {}).get("title") or "BookText document"]
    named = {
        fmt: str(document_identity.register_deliverable(root, paths[fmt].relative_to(root), f"publication {fmt.upper()}", sources=identity_sources))
        for fmt in requested
    }
    block_counts = {kind: sum(block["type"] == kind for block in document["blocks"]) for kind in ("unknown", "table")}
    visual_mode = "classified_figures" if has_figures else ("source_page_facsimile_fallback" if source_page_assets else "no_visual_evidence")
    warnings = list(document.get("warnings", []))
    if visual_mode == "no_visual_evidence":
        warnings.append({"kind": "no_visual_evidence_for_docx", "message": "CDM has no figure blocks or retained source-page assets; DOCX cannot safely add images."})
    manifest = {"schema_version": EXPORT_SCHEMA, "artifact_type": "booktext.export_manifest", "cdm": {"path": "book_document.json", "sha256": _sha(document_path), "schema_version": document["schema_version"]}, "booktext_version": document["metadata"].get("booktext_version"), "generated": {fmt: {"path": str(paths[fmt].relative_to(root)), "named_deliverable": named[fmt], **validations[fmt]} for fmt in requested}, "validation": {"cdm": "PASS", "formats": {fmt: "PASS" for fmt in requested}}, "warnings": warnings, "visual_preservation": {"mode": visual_mode, "source_page_facsimile_count": len(source_page_assets), "policy": "Source-page facsimiles are page provenance only, never inferred figures or captions."}, "unresolved_priority_counts": document.get("review", {}).get("unresolved_priority_counts", {}), "fallback_unknown_counts": {"unknown": block_counts["unknown"], "fallback_tables": sum(block.get("table_status") == "fallback_review" for block in document["blocks"] if block["type"] == "table")}, "policy": "Publication outputs are derived from CDM only. CDM remains the canonical machine source for Integra Evidence ingestion."}
    _write(export_dir / "export_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


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
    for block in document["blocks"]:
        kind = block["type"]
        if kind == "page_break": story.append(PageBreak())
        elif kind == "heading": story.extend([Paragraph(block.get("text", ""), styles["Heading2"]), Spacer(1, 8)])
        elif kind in {"paragraph", "list", "unknown"}:
            prefix = "• " if kind == "list" else ("Unknown layout: " if kind == "unknown" else "")
            story.extend([Paragraph(prefix + html.escape(block.get("text", "")).replace("\n", "<br/>"), styles["BodyText"]), Spacer(1, 5)])
        elif kind == "table":
            if block.get("table_status") == "fallback_review": story.extend([Paragraph("Table fallback review — no verified cells are available.", styles["Italic"]), Spacer(1, 5)])
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


def main(argv=None):
    if argv and argv[0] == "_render_pdf": _pdf_main(Path(argv[1]), Path(argv[2])); return
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("output_dir", type=Path); parser.add_argument("--format", action="append", choices=[*FORMATS, "all"])
    args = parser.parse_args(argv); print(json.dumps(export_document(args.output_dir, args.format or ["all"]), ensure_ascii=False))


if __name__ == "__main__": main(sys.argv[1:])
