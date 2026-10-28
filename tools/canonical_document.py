#!/usr/bin/env python3
"""Build and validate BookText's conservative canonical document model (CDM).

The CDM is an offline projection of existing BookText artifacts.  It never
starts OCR, a model, or an external process; raw OCR remains in ``book.json``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
from collections import Counter
from pathlib import Path

from tools import review_decisions, text_normalization, document_continuity


SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "booktext.canonical_document"
BLOCK_TYPES = {"heading", "paragraph", "list", "table", "figure", "caption", "page_break", "unknown"}
NON_PUBLICATION_LAYOUT_LABELS = {"pageheader", "pagefooter", "pagenumber"}


def _hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _hash_file(path):
    return _hash_bytes(Path(path).read_bytes())


def _stable_id(kind, value):
    return f"{kind}-{_hash_bytes(str(value).encode('utf-8'))[:20]}"


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON {path}: {error}") from error


def _relative_if_file(root, value):
    if not isinstance(value, str) or not value:
        return None
    path = root / value
    return value if path.is_file() else None


def _surya_pages(root):
    path = root / "surya" / "surya-input" / "results.json"
    if not path.is_file():
        return {}, None
    payload = _load(path)
    pages = payload.get("pages") if isinstance(payload, dict) else None
    index = {}
    if isinstance(pages, list):
        for value in pages:
            if isinstance(value, dict) and isinstance(value.get("page"), int):
                index[value["page"]] = value
    elif isinstance(payload, dict):
        # Surya's production result is keyed as ``page-0001`` in some
        # releases; retain compatibility without changing the raw artifact.
        for key, value in payload.items():
            match = re.fullmatch(r"page-(\d+)", str(key))
            if match and isinstance(value, list) and len(value) == 1:
                value = value[0]
            if match and isinstance(value, dict):
                index[int(match.group(1))] = value
    return index, "surya/surya-input/results.json"


def _recovered_visuals(root):
    """Read offline crop evidence without promoting it to OCR-derived text."""
    path = root / "visual_evidence.json"
    if not path.is_file():
        return {}, None
    payload = _load(path)
    if payload.get("schema_version") != "1.0" or payload.get("artifact_type") != "booktext.recovered_visual_evidence":
        raise ValueError("unsupported recovered visual evidence schema/version")
    index = {}
    for visual in payload.get("visuals", []):
        if isinstance(visual, dict) and isinstance(visual.get("page"), int):
            index.setdefault(visual["page"], []).append(visual)
    return index, "visual_evidence.json"


def _block_text(block):
    if not block.get("html"):
        return str(block.get("text") or block.get("markdown") or "").strip()
    value = block["html"]
    text = re.sub(r"<(?:br|/p|/div|/li)\b[^>]*>", "\n", str(value), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines() if line.strip())


def _type_for_label(label):
    label = str(label or "").replace("_", "").replace("-", "").lower()
    if label in {"sectionheader", "section header", "title", "header"}:
        return "heading"
    if label in {"text", "paragraph", "body"}:
        return "paragraph"
    if label in {"list", "listitem", "list item"}:
        return "list"
    return "unknown"


def _is_running_matter(label):
    """True only for layout-labelled running heads, feet, and page numbers.

    This deliberately does not include ``SectionHeader``/``Header``: those
    can be authorial chapter and section titles and must remain in the book.
    """
    return str(label or "").replace("_", "").replace("-", "").casefold() in NON_PUBLICATION_LAYOUT_LABELS


def _line_key(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _repeated_running_lines(pages):
    """Find only strong cross-page header/footer evidence in legacy page text.

    This is a fallback for outputs without saved layout blocks (for example,
    native PDF text). A candidate must recur on at least three pages in the
    first/last three nonblank lines and be short enough to be running matter.
    """
    counts = Counter()
    for page in pages:
        lines = [line.strip() for line in str(page.get("text", "")).splitlines() if line.strip()]
        seen = set()
        for line in [*lines[:3], *lines[-3:]]:
            key = _line_key(line)
            if key and not key.isdecimal() and len(key) <= 120 and len(key.split()) <= 16:
                seen.add(key)
        counts.update(seen)
    return {line for line, count in counts.items() if count >= 3}


def _without_running_matter(text, repeated_lines):
    lines = [line for line in str(text or "").splitlines()]
    nonblank_positions = [index for index, line in enumerate(lines) if line.strip()]
    boundary_positions = set(nonblank_positions[:3] + nonblank_positions[-3:])
    kept = []
    for index, line in enumerate(lines):
        key = _line_key(line)
        # Numbers at a page boundary and demonstrably repeated short lines are
        # page furniture, not source prose. Never remove a body occurrence.
        if index in boundary_positions and (key.isdecimal() or key in repeated_lines):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _fallback_text_blocks(text):
    """Project page text without saved layout blocks conservatively.

    Native-text and Tesseract-only runs retain paragraph breaks, even when they
    do not retain Surya layout blocks.  Those breaks are sufficient evidence for
    ordinary prose paragraphs; representing every page as ``unknown`` makes a
    readable final book look like a developer diagnostic.  We only promote a
    paragraph to a heading when its textual form is independently strong.  All
    other content remains a paragraph rather than acquiring invented semantics.
    """
    for value in re.split(r"\n\s*\n", str(text or "").strip()):
        value = value.strip()
        if not value:
            continue
        compact = " ".join(line.strip() for line in value.splitlines() if line.strip())
        letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", compact)
        explicit_article = re.fullmatch(r"(?:статья|article)\s+\d+[A-Za-zА-Яа-яЁё.-]*", compact, flags=re.I)
        uppercase_short = (
            len(compact) <= 120 and bool(letters) and letters == letters.upper()
            and len(letters) >= 3
        )
        yield ("heading" if explicit_article or uppercase_short else "paragraph"), value


def _native_fragment(raw, lines):
    """Keep an exact subset of a saved native-layout block as native evidence."""
    if not lines:
        return None
    bounds = [min(line["bbox"][0] for line in lines), min(line["bbox"][1] for line in lines),
              max(line["bbox"][2] for line in lines), max(line["bbox"][3] for line in lines)]
    return {**raw, "bbox": bounds, "lines": lines, "text": "\n".join(line["text"] for line in lines)}


def _native_mapping_rows(lines):
    """Return a conservative, geometry-verified native two-column table run.

    The source is a PDF text layer: every accepted cell is an existing native
    text line.  We require at least three consecutive, two-line rows with
    aligned baselines and a material column gap.  An ambiguous adjacent line
    is deliberately *not* absorbed into a cell: it remains ordinary source
    text, rather than being guessed into a grid.
    """
    if len(lines) < 3:
        return None
    heights = [line["bbox"][3] - line["bbox"][1] for line in lines]
    median_height = sorted(heights)[len(heights) // 2] if heights else 0
    tolerance = max(1.0, median_height * .45)
    rows, index = [], 0
    while index < len(lines):
        row, baseline = [lines[index]], lines[index]["bbox"][1]
        index += 1
        while index < len(lines) and abs(lines[index]["bbox"][1] - baseline) <= tolerance:
            row.append(lines[index]); index += 1
        if len(row) == 2:
            left, right = sorted(row, key=lambda value: value["bbox"][0])
            gap = right["bbox"][0] - left["bbox"][2]
            short_mapping_terms = all(1 <= len(str(value.get("text", "")).split()) <= 5 for value in (left, right))
            if gap >= max(12.0, median_height * 1.4) and short_mapping_terms:
                rows.append((lines.index(left), lines.index(right), left, right))
                continue
        rows.append(None)
    best = None
    for start, row in enumerate(rows):
        if row is None:
            continue
        end = start
        while end + 1 < len(rows) and rows[end + 1] is not None:
            previous, following = rows[end], rows[end + 1]
            if following[2]["bbox"][1] - previous[2]["bbox"][3] > max(14.0, median_height * 2.2):
                break
            end += 1
        if end - start + 1 >= 3:
            candidate = rows[start:end + 1]
            if best is None or len(candidate) > len(best):
                best = candidate
    if best is None:
        return None
    first = min(row[0] for row in best); last = max(row[1] for row in best)
    grid = [[str(row[2]["text"]).strip(), str(row[3]["text"]).strip()] for row in best]
    # A native PDF can occasionally store the final two-column row as a
    # single text line.  Keep that exact line *inside* the table, while never
    # guessing where its two cells divide.  The repeated left-column prefix,
    # vertical adjacency, and matching row width make this a table member;
    # its second cell is intentionally empty rather than fabricated.
    if last + 1 < len(lines):
        continuation = lines[last + 1]
        left_edge = min(row[2]["bbox"][0] for row in best)
        right_edge = max(row[3]["bbox"][2] for row in best)
        close = continuation["bbox"][1] - lines[last]["bbox"][3] <= max(14.0, median_height * 2.2)
        # A first row may have a longer left label and therefore start farther
        # left than the repeated data rows.  Align against the data-row median.
        data_lefts = sorted(row[2]["bbox"][0] for row in best[1:]) or [left_edge]
        data_left = data_lefts[len(data_lefts) // 2]
        aligned = abs(continuation["bbox"][0] - data_left) <= max(8.0, median_height * 1.2)
        within = continuation["bbox"][2] <= right_edge + max(12.0, median_height * 1.4)
        prefixes = {re.match(r"^\s*([A-Za-z]+)", row[0]).group(1).casefold()
                    for row in grid[1:] if re.match(r"^\s*([A-Za-z]+)", row[0])}
        text = str(continuation.get("text", "")).strip()
        token = re.match(r"^\s*([A-Za-z]+)", text)
        shape_tail = re.search(r"\b(?:a|an)\s+[A-Za-z][A-Za-z -]*$", text, flags=re.I)
        if close and aligned and within and token and token.group(1).casefold() in prefixes and shape_tail:
            # Prefer actual native *word* geometry over a textual split.  The
            # established right column provides an observed boundary; only a
            # material word gap across that boundary becomes two cells.
            right_starts = sorted(row[3]["bbox"][0] for row in best)
            right_start = right_starts[len(right_starts) // 2]
            words = [word for word in continuation.get("words", [])
                     if isinstance(word, dict) and isinstance(word.get("bbox"), list) and len(word["bbox"]) == 4
                     and str(word.get("text", "")).strip()]
            split = next((position for position, word in enumerate(words)
                          if word["bbox"][0] >= right_start - max(1.0, median_height * .2)), None)
            if split and words[split - 1]["bbox"][2] < words[split]["bbox"][0] - max(1.0, median_height * .5):
                grid.append([" ".join(str(word["text"]).strip() for word in words[:split]),
                             " ".join(str(word["text"]).strip() for word in words[split:])])
            else:
                grid.append([text, ""])
            last += 1
    return first, last, grid


def _write_native_table_artifacts(root, *, page, raw_index, grid, bbox, page_ref):
    """Persist a verified native-PDF table using the structured table contract."""
    tables = root / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    stem = f"page-{page:04d}-native-table-{raw_index:02d}"
    paths = {kind: tables / f"{stem}.{kind}" for kind in ("json", "tsv", "html")}
    payload = {
        "schema_version": "1.0", "artifact_type": "booktext.native_pdf_geometry_table",
        "source": "native_pdf_words_geometry", "page": page, "table_index": raw_index,
        "bbox": bbox, "rows": grid, "grid": grid, "row_count": len(grid), "column_count": 2,
        "provenance": {"native_evidence": page_ref.get("raw_native_ref"),
                       "cell_status": "verified_native_text_geometry; unsplit native rows retain one empty cell"},
    }
    paths["json"].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with paths["tsv"].open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, delimiter="\t").writerows(grid)
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in grid)
    paths["html"].write_text(f"<table>{rows}</table>\n", encoding="utf-8")
    return {kind: f"tables/{path.name}" for kind, path in paths.items()}


def _final_pages(book, priority, decisions, book_path, priority_path, decisions_path, version):
    """Use the existing #33 semantics, then recover only final per-page text."""
    if not Path(decisions_path).is_file():
        items = review_decisions._priority_index(priority)
        unresolved = Counter(item.get("priority") for item in items.values() if item.get("priority") in {"HIGH", "MEDIUM"})
        return ({page["page"]: str(page.get("text", "")) for page in review_decisions._page_index(book).values()}, {
            "decision_status_counts": {}, "applied_text_changes": 0, "stale_or_conflict_count": 0,
            "unresolved_priority_counts": {"HIGH": unresolved["HIGH"], "MEDIUM": unresolved["MEDIUM"]}, "decisions": [],
        })
    final_text, _markdown, report = review_decisions.build_final(
        book, priority, decisions, book_path=book_path, priority_path=priority_path,
        decisions_path=decisions_path, booktext_version=version,
    )
    chunks = re.split(r"^===== СТРАНИЦА (\d+) =====\n\n", final_text, flags=re.M)
    pages = {}
    for index in range(1, len(chunks), 2):
        pages[int(chunks[index])] = chunks[index + 1].rstrip("\n")
    return pages, report


def _decision_index(report, decisions):
    statuses = {entry.get("item_id"): entry for entry in report.get("decisions", []) if isinstance(entry, dict)}
    # Early #33 decision artifacts did not persist ``decision_id``.  The item
    # ID is still the source-of-truth key and is a safe backward-compatible
    # provenance reference, never a generated correction.
    decision_ids = {entry.get("item_id"): entry.get("decision_id") or f"item:{entry.get('item_id')}" for entry in decisions.get("decisions", []) if isinstance(entry, dict)}
    decision_pages = {entry.get("item_id"): entry.get("page") for entry in decisions.get("decisions", []) if isinstance(entry, dict)}
    by_page = {}
    for item_id, entry in statuses.items():
        if entry.get("status") == "resolved_changed":
            by_page.setdefault(decision_pages.get(item_id), []).append((item_id, decision_ids.get(item_id)))
    return by_page


def build_document(output_dir, *, booktext_version="0.1.0"):
    """Build deterministic ``book_document.json`` from one existing output dir."""
    root = Path(output_dir).expanduser().resolve()
    book_path, priority_path = root / "book.json", root / "review_priority.json"
    if not book_path.is_file() or not priority_path.is_file():
        raise ValueError("CDM build requires book.json and review_priority.json")
    book, priority = _load(book_path), _load(priority_path)
    selection = document_continuity.selection(root, book)
    if book.get("extraction_policy") in {"automatic-extraction-1", "automatic-extraction-2"} and (root / "extraction/source.json").is_file():
        expected_artifacts = _load(root / "extraction/artifacts.json")
        for relative,digest in expected_artifacts.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file() or _hash_file(path) != digest:
                raise ValueError(f"stale extraction evidence: {relative}")
    decisions_path = root / "review_decisions.json"
    decisions = _load(decisions_path) if decisions_path.is_file() else review_decisions.empty_decisions(priority_path, book_path)
    final_pages, final_report = _final_pages(book, priority, decisions, book_path, priority_path, decisions_path, booktext_version)
    surya, raw_surya_ref = _surya_pages(root)
    recovered_visuals, recovered_visual_ref = _recovered_visuals(root)
    pages = sorted(book.get("pages", []), key=lambda value: value.get("page", 0))
    repeated_running_lines = _repeated_running_lines(pages)
    page_numbers = [value.get("page") for value in pages]
    if any(not isinstance(value, int) for value in page_numbers) or len(set(page_numbers)) != len(page_numbers):
        raise ValueError("book pages must have unique numeric provenance for CDM")
    changed_by_page = _decision_index(final_report, decisions)
    status_by_item = {entry.get("item_id"): entry.get("status") for entry in final_report.get("decisions", []) if isinstance(entry, dict)}
    review_by_page = {}
    for item in priority.get("items", []):
        if isinstance(item, dict) and isinstance(item.get("page"), int):
            review_by_page.setdefault(item["page"], []).append((item.get("item_id"), status_by_item.get(item.get("item_id"), "unresolved")))
    blocks, source_pages, warnings = [], [], []
    warnings.extend({"kind": "intentional_excerpt_gap", **gap, "message": document_continuity.gap_label(gap)} for gap in selection["gaps"])
    if selection["completeness"] != "verified":
        warnings.append({"kind": "unverifiable_legacy_selection", "message": "Original requested page selection is unavailable; completeness cannot be verified."})
    order = 0
    document_id = _stable_id("document", _hash_file(book_path))

    def add(block):
        nonlocal order
        order += 1
        block["reading_order"] = order
        blocks.append(block)

    for record in pages:
        page = record["page"]
        warnings.extend(record.get("extraction_warnings", []))
        page_ref = {"source_page": page, "prepared_page": _relative_if_file(root, record.get("page_image")), "raw_ocr_ref": raw_surya_ref}
        if record.get("layout_blocks") is not None:
            page_ref["raw_native_ref"] = _relative_if_file(root, (record.get("source_provenance") or {}).get("native_evidence"))
            page_ref["source_sha256"] = (record.get("source_provenance") or {}).get("source_sha256")
        item_states = review_by_page.get(page, [])
        source_pages.append({**page_ref, "review": {"item_ids": [item_id for item_id, _status in item_states], "status_counts": dict(Counter(status for _item_id, status in item_states))}})
        if record.get("page_image") and page_ref["prepared_page"] is None:
            warnings.append({"page": page, "kind": "missing_prepared_page_asset", "path": record["page_image"]})
        page_blocks = (surya.get(page) or {}).get("blocks")
        native_blocks = record.get("layout_blocks")
        if isinstance(native_blocks, list):
            page_blocks = [*(page_blocks or []), *native_blocks]
            # Native points and OCR pixels need a common coordinate system.
            dimensions = (record.get("source_provenance") or {}).get("page_size_points")
            if dimensions and page_ref["prepared_page"]:
                from PIL import Image
                with Image.open(root / page_ref["prepared_page"]) as image:
                    image_box = (surya.get(page) or {}).get("image_bbox") or [0,0,image.width,image.height]
                    sx,sy = dimensions[0]/(image_box[2]-image_box[0]), dimensions[1]/(image_box[3]-image_box[1])
                page_blocks = [{**raw, "bbox": [v*(sx if i%2==0 else sy) for i,v in enumerate(raw["bbox"])]}
                               if raw.get("bbox") and raw not in native_blocks else raw for raw in page_blocks]
            # Preserve per-column native flow; insert OCR regions by vertical location.
            if native_blocks and len(page_blocks) == len(native_blocks):
                page_blocks = native_blocks
            elif native_blocks:
                page_blocks = sorted(page_blocks, key=lambda raw: ((raw.get("bbox") or [0,0])[1], (raw.get("bbox") or [0,0])[0]))
            else:
                # A scan with an evidence-only OCR layer has no visible native
                # blocks. Preserve Surya's reading order: sorting by y then x
                # interleaves columns in journals and destroys paragraph flow.
                page_blocks = sorted(page_blocks, key=lambda raw: raw.get("reading_order", 0))
            page_blocks = [{**raw, "reading_order": i} for i,raw in enumerate(page_blocks, 1)]
            if not page_blocks and record.get("text", "").strip():
                # An OCR engine/version without blocks still owns real text.
                # Empty native layout must not suppress the safe text fallback.
                page_blocks = None
        has_changed_text = bool(changed_by_page.get(page))
        table_entries = (record.get("tables") or {}).get("tables") or []
        tables_by_index = {value.get("index"): value for value in table_entries if isinstance(value, dict)}
        emitted_tables = set()
        visuals = [*(record.get("visuals") or []), *recovered_visuals.get(page, [])]
        emitted_visuals = set()

        def add_visual(visual):
            identity = visual.get("image") or visual.get("index")
            if identity in emitted_visuals:
                return
            emitted_visuals.add(identity)
            dimensions = (record.get("source_provenance") or {}).get("page_size_points")
            if page_ref["prepared_page"] and dimensions:
                from tools.automatic_extraction import refine_scanned_figure
                visual, framing_warnings = refine_scanned_figure(root, visual, page_ref["prepared_page"], dimensions)
                warnings.extend({"page": page, **warning} for warning in framing_warnings)
            image = _relative_if_file(root, visual.get("image"))
            if image is None:
                warnings.append({"page": page, "kind": "missing_visual_asset", "path": visual.get("image")})
                return
            add({"block_id": _stable_id("figure", f"{document_id}:{page}:{identity}"), "type": "figure", "image": image,
                 "description": visual.get("description"), "description_ref": _relative_if_file(root, visual.get("description_file")), "caption_id": None,
                 "provenance": {**page_ref, "visual_index": visual.get("index"), "visual_type": visual.get("type"), "bbox": visual.get("bbox"), "recovered_visual_evidence": recovered_visual_ref if visual.get("source_provenance") else None, "source": visual.get("source_provenance")}})

        def add_table(table):
            status, index = table.get("status"), table.get("index")
            if index in emitted_tables:
                return
            emitted_tables.add(index)
            provenance = {**page_ref, "table_index": index, "bbox": table.get("bbox"), "confidence": table.get("confidence"), "source": table.get("source")}
            if status == "HTML_TABLE":
                artifacts = {name: _relative_if_file(root / "tables", table.get(name)) for name in ("json", "tsv", "html")}
                if any(value is None for value in artifacts.values()):
                    raise ValueError(f"structured table page {page} index {index} has missing artifact")
                add({"block_id": _stable_id("table", f"{document_id}:{page}:{index}"), "type": "table", "table_status": "structured", "rows": table.get("rows"), "columns": table.get("columns"), "artifacts": {name: f"tables/{value}" for name, value in artifacts.items()}, "provenance": provenance})
            elif status == "fallback_review":
                add({"block_id": _stable_id("table", f"{document_id}:{page}:{index}"), "type": "table", "table_status": "fallback_review", "review_required": True, "diagnostic": table.get("diagnostic"), "provenance": provenance})
        page_start = len(blocks)
        if has_changed_text or not isinstance(page_blocks, list):
            text = _without_running_matter(final_pages.get(page, str(record.get("text", ""))), repeated_running_lines)
            fallback_reason = "valid_corrections_require_page_text_projection" if has_changed_text else "layout_blocks_unavailable"
            for fallback_index, (kind, fragment) in enumerate(_fallback_text_blocks(text), start=1):
                add({"block_id": _stable_id("block", f"{document_id}:{page}:page-text:{fallback_index}"), "type": kind, "text": fragment,
                     "level": None if kind == "heading" else None,
                     "provenance": {**page_ref, "raw_page_text_sha256": _hash_bytes(str(record.get("text", "")).encode()), "correction_decision_ids": [did for _item, did in changed_by_page.get(page, []) if did], "fallback_reason": fallback_reason}})
        else:
            raw_table_index = 0
            for index, raw in enumerate(sorted((value for value in page_blocks if isinstance(value, dict)), key=lambda value: value.get("reading_order", 0)), start=1):
                label = str(raw.get("label", ""))
                if _is_running_matter(label):
                    continue
                if label.lower() == "table":
                    raw_table_index += 1
                    if raw_table_index in tables_by_index:
                        add_table(tables_by_index[raw_table_index])
                    continue
                if label.lower() == "figure":
                    # A retained primary-layout crop belongs at this exact
                    # primary reading position, not at a y-sorted page tail.
                    # Match anchored crop geometry only, never guess an image.
                    bounds = raw.get("bbox")
                    for visual in visuals:
                        source = visual.get("source_provenance") or {}
                        other = visual.get("bbox")
                        if (source.get("kind") == "surya_layout_source_crop" and
                                source.get("raw_layout") == raw_surya_ref and
                                isinstance(bounds, list) and isinstance(other, list) and
                                len(bounds) == len(other) == 4 and
                                all(isinstance(a, (int, float)) and isinstance(b, (int, float)) and
                                    abs(a-b) < .01 for a,b in zip(bounds,other))):
                            add_visual(visual)
                    continue
                dimensions = (record.get("source_provenance") or {}).get("page_size_points")
                segments = [("text", raw)]
                if (label.casefold() in {"text", "paragraph", "body"} and raw.get("extraction") == "native_pdf_words"
                        and isinstance(raw.get("lines"), list) and dimensions):
                    mapping = _native_mapping_rows(raw["lines"])
                    if mapping:
                        first, last, grid = mapping
                        bounds = [min(line["bbox"][0] for line in raw["lines"][first:last+1]),
                                  min(line["bbox"][1] for line in raw["lines"][first:last+1]),
                                  max(line["bbox"][2] for line in raw["lines"][first:last+1]),
                                  max(line["bbox"][3] for line in raw["lines"][first:last+1])]
                        artifacts = _write_native_table_artifacts(root, page=page, raw_index=index, grid=grid,
                                                                  bbox=bounds, page_ref=page_ref)
                        segments = []
                        before, after = _native_fragment(raw, raw["lines"][:first]), _native_fragment(raw, raw["lines"][last+1:])
                        if before: segments.append(("text", before))
                        segments.append(("native_table", {"grid": grid, "bbox": bounds, "artifacts": artifacts,
                            "line_start": first + 1, "line_end": last + 1}))
                        if after: segments.append(("text", after))
                for segment_index, (segment_kind, value) in enumerate(segments, start=1):
                    if segment_kind == "native_table":
                        add({"block_id": _stable_id("table", f"{document_id}:{page}:native-layout:{index}:{value['line_start']}-{value['line_end']}"),
                             "type": "table", "table_status": "structured", "rows": len(value["grid"]), "columns": 2,
                             "artifacts": value["artifacts"],
                             "provenance": {**page_ref, "layout_label": label, "layout_reading_order": raw.get("reading_order"),
                                            "bbox": value["bbox"], "raw_ocr_ref": raw_surya_ref,
                                            "native_line_range": [value["line_start"], value["line_end"]],
                                            "source": {"kind": "native_pdf_words_geometry", "source_page": page,
                                                       "native_evidence": page_ref.get("raw_native_ref"),
                                                       "cell_status": "verified_native_text_geometry"}}})
                        continue
                    text = _block_text(value)
                    if not text:
                        continue
                    kind = _type_for_label(label)
                    fragments = [value]
                    if kind == "paragraph" and value.get("extraction") == "native_pdf_words" and value.get("lines") and dimensions:
                        # Reuse saved word geometry, even for an older output. Raw
                        # OCR/native artifacts and human decision anchors stay put.
                        from tools.automatic_extraction import native_layout
                        fragments = native_layout({"blocks": [value]}, [])
                    for part, fragment in enumerate(fragments):
                        suffix = f":segment:{segment_index}" + ("" if part == 0 else f":paragraph:{part+1}")
                        add({"block_id": _stable_id("block", f"{document_id}:{page}:raw:{index}{suffix}"), "type": kind, "text": _block_text(fragment),
                             "level": None if kind == "heading" else None,
                             "provenance": {**page_ref, "layout_label": label, "layout_reading_order": raw.get("reading_order"), "bbox": fragment.get("bbox"), "polygon": raw.get("polygon"), "confidence": raw.get("confidence"), "raw_ocr_ref": raw_surya_ref,
                                            "native_paragraph_part": part+1 if len(fragments)>1 else None,
                                            "line_geometry": text_normalization.line_geometry(fragment.get("lines"), dimensions, fragment.get("bbox")) if value.get("extraction") == "native_pdf_words" else None}})
        for table in sorted(tables_by_index.values(), key=lambda value: value.get("index", 0)):
            add_table(table)
        for visual in visuals:
            add_visual(visual)
        if native_blocks and not has_changed_text:
            page_content = blocks[page_start:]
            figures = [block for block in page_content if block["type"] == "figure"]
            content = [block for block in page_content if block["type"] != "figure"]
            for figure in figures:
                bounds = figure["provenance"].get("bbox")
                position = len(content)
                if bounds:
                    for i,block in enumerate(content):
                        other = block["provenance"].get("bbox")
                        if other and other[1] >= bounds[3]: position = i; break
                content.insert(position, figure)
            blocks[page_start:] = content
            for i,block in enumerate(blocks, 1): block["reading_order"] = i
        gap = next((gap for gap in selection["gaps"] if gap["after_page"] == page), None)
        add({"block_id": _stable_id("break", f"{document_id}:{page}"), "type": "page_break", "provenance": page_ref,
             **({"boundary_kind": "excerpt_gap", "text": document_continuity.gap_label(gap), "gap": gap} if gap else {})})
    reference_texts = [final_pages.get(record["page"], record.get("text", "")) for record in pages]
    native_evidence = root / "extraction/native.xml"
    if book.get("extraction_policy") == "automatic-extraction-1" and native_evidence.is_file():
        from tools.automatic_extraction import parse_native
        reference_texts = [" ".join(word["text"] for block in native["blocks"] for line in block["lines"] for word in line["words"])
                            for native in parse_native(native_evidence.read_text())]
    normalization = text_normalization.project(blocks, reference_texts)
    warnings.extend(normalization["review_candidates"])
    if native_evidence.is_file(): normalization["reference_evidence"] = {"path": "extraction/native.xml", "sha256": _hash_file(native_evidence)}
    unresolved = final_report.get("unresolved_priority_counts", {})
    source_document = book.get("document") if isinstance(book.get("document"), dict) else {}
    source_files = [Path(value).name for value in book.get("sources", []) if isinstance(value, str)]
    document_title = source_document.get("title") or (Path(source_files[0]).stem if source_files else None)
    document = {
        "schema_version": SCHEMA_VERSION, "artifact_type": ARTIFACT_TYPE, "document_id": document_id,
        "metadata": {"title": document_title, "author": None, "booktext_version": booktext_version, "source_page_count": len(pages), "languages": book.get("ocr_languages"), "ocr_engine": book.get("ocr_engine"), "source_files": source_files, "build_policy": "existing artifacts only; no OCR or model inference"},
        "source": {"book_json": {"path": "book.json", "sha256": _hash_file(book_path)}, "document_identity": {"path": source_document.get("identity_artifact"), "title": document_title}, "review_priority": {"path": "review_priority.json", "sha256": _hash_file(priority_path)}, "review_decisions": {"path": decisions_path.name, "sha256": _hash_file(decisions_path) if decisions_path.is_file() else None}, "recovered_visual_evidence": {"path": recovered_visual_ref, "sha256": _hash_file(root / recovered_visual_ref) if recovered_visual_ref else None}},
        "review": {"final_build": {"decision_status_counts": final_report.get("decision_status_counts", {}), "applied_text_changes": final_report.get("applied_text_changes", 0), "stale_or_conflict_count": final_report.get("stale_or_conflict_count", 0)}, "unresolved_priority_counts": {"HIGH": unresolved.get("HIGH", 0), "MEDIUM": unresolved.get("MEDIUM", 0)}},
        "source_pages": source_pages, "selection": selection, "blocks": blocks, "normalization": normalization, "notes": [], "warnings": warnings,
    }
    document["metadata"]["selection_notice"] = document_continuity.selection_notice(selection)
    validate_document(document, root=root, decisions=decisions)
    return document


def validate_document(document, *, root=None, decisions=None):
    if document.get("schema_version") != SCHEMA_VERSION or document.get("artifact_type") != ARTIFACT_TYPE:
        raise ValueError("unsupported CDM schema/version")
    if not isinstance(document.get("blocks"), list) or not isinstance(document.get("source_pages"), list):
        raise ValueError("CDM requires source_pages and blocks lists")
    source_page_order = [entry.get("source_page") for entry in document["source_pages"]]
    source_pages = set(source_page_order)
    if any(not isinstance(page, int) for page in source_page_order) or source_page_order != sorted(source_page_order) or len(source_pages) != len(source_page_order):
        raise ValueError("invalid source page provenance")
    selection = document.get("selection")
    if selection:
        expected = selection.get("requested_pages")
        if selection.get("actual_pages") != source_page_order or (expected is not None and expected != source_page_order):
            raise ValueError("CDM page completeness failure")
        expected_gaps = [{"after_page": a, "before_page": b, "omitted_start": a+1, "omitted_end": b-1}
                         for a,b in zip(source_page_order, source_page_order[1:]) if b > a+1]
        if selection.get("gaps") != expected_gaps:
            raise ValueError("invalid excerpt gap provenance")
        breaks = [block for block in document["blocks"] if block.get("type") == "page_break"]
        if [block.get("provenance", {}).get("source_page") for block in breaks] != source_page_order:
            raise ValueError("missing/duplicate source page boundary")
        if [block.get("gap") for block in breaks if block.get("boundary_kind") == "excerpt_gap"] != expected_gaps:
            raise ValueError("missing/invalid excerpt boundary")
        for block in breaks:
            if block.get("boundary_kind") == "excerpt_gap":
                gap = block["gap"]
                if block.get("text") != document_continuity.gap_label(gap) or block["provenance"]["source_page"] != gap["after_page"]:
                    raise ValueError("invalid excerpt boundary label/provenance")
    ids, expected_order = set(), 1
    decision_ids = {(entry.get("decision_id") or f"item:{entry.get('item_id')}") for entry in (decisions or {}).get("decisions", []) if isinstance(entry, dict)}
    for block in document["blocks"]:
        if block.get("type") not in BLOCK_TYPES:
            raise ValueError("unsupported block type")
        if not isinstance(block.get("block_id"), str) or block["block_id"] in ids:
            raise ValueError("duplicate or invalid block ID")
        ids.add(block["block_id"])
        if block.get("reading_order") != expected_order:
            raise ValueError("invalid reading order")
        expected_order += 1
        provenance = block.get("provenance") or {}
        if provenance.get("source_page") not in source_pages:
            raise ValueError("block has invalid page provenance")
        if block["type"] == "table":
            if block.get("table_status") == "structured":
                if "cells" in block or set((block.get("artifacts") or {})) != {"json", "tsv", "html"}:
                    raise ValueError("invalid structured table reference")
                for path in block["artifacts"].values():
                    if root is not None and not (Path(root) / path).is_file():
                        raise ValueError("missing structured table artifact")
            elif block.get("table_status") == "fallback_review":
                if "cells" in block:
                    raise ValueError("fallback table must not contain cells")
                visual = block.get("fallback_visual")
                if visual and root is not None and not (Path(root) / visual).is_file():
                    raise ValueError("broken fallback table visual reference")
            else:
                raise ValueError("unsupported table status")
        for decision_id in provenance.get("correction_decision_ids", []):
            if decision_id not in decision_ids:
                raise ValueError("decision provenance points to nonexistent decision")
        for key in ("prepared_page", "raw_ocr_ref", "raw_native_ref"):
            value = provenance.get(key)
            if value and root is not None and not (Path(root) / value).is_file():
                raise ValueError("broken artifact reference")
        if block.get("type") == "figure" and root is not None and not (Path(root) / block.get("image", "")).is_file():
            raise ValueError("broken figure artifact reference")
    by_id = {block["block_id"]: block for block in document["blocks"]}
    for block in document["blocks"]:
        provenance = block["provenance"]
        owner_id = provenance.get("text_consumed_by")
        if owner_id:
            owner = by_id.get(owner_id)
            if (not owner or block.get("text") or not owner.get("text")
                    or block["block_id"] not in owner["provenance"].get("continuation_block_ids", [])
                    or provenance["source_page"] not in owner["provenance"].get("source_pages", [])):
                raise ValueError("invalid paragraph continuation ownership")
        continuations = provenance.get("continuation_block_ids", [])
        if continuations:
            page_chain = provenance.get("source_pages", [])
            if (len(continuations) != len(set(continuations)) or len(page_chain) != len(continuations)+1
                    or page_chain != list(range(provenance["source_page"], provenance["source_page"]+len(page_chain)))
                    or any(identifier not in by_id or by_id[identifier]["provenance"].get("text_consumed_by") != block["block_id"] for identifier in continuations)
                    or [by_id[identifier]["provenance"]["source_page"] for identifier in continuations] != page_chain[1:]):
                raise ValueError("broken/cross-gap paragraph continuation provenance")
    return {"valid": True, "block_count": len(document["blocks"]), "warnings": len(document.get("warnings", []))}


def write_document(document, output_dir):
    path = Path(output_dir) / "book_document.json"
    review_decisions.atomic_write_json(path, document)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build CDM from an existing BookText output directory")
    build.add_argument("output_dir", type=Path)
    build.add_argument("--booktext-version", default="0.1.0")
    check = sub.add_parser("validate", help="Validate an existing book_document.json")
    check.add_argument("document", type=Path)
    check.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        document = build_document(args.output_dir, booktext_version=args.booktext_version)
        path = write_document(document, args.output_dir)
        print(json.dumps({"path": str(path), "blocks": len(document["blocks"]), "warnings": len(document["warnings"])}, ensure_ascii=False))
    else:
        document = _load(args.document)
        root = args.output_dir.expanduser().resolve() if args.output_dir else None
        decisions_path = root / "review_decisions.json" if root else None
        decisions = _load(decisions_path) if decisions_path and decisions_path.is_file() else None
        print(json.dumps(validate_document(document, root=root, decisions=decisions), ensure_ascii=False))


if __name__ == "__main__":
    main()
