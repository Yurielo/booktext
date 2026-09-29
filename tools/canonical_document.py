#!/usr/bin/env python3
"""Build and validate BookText's conservative canonical document model (CDM).

The CDM is an offline projection of existing BookText artifacts.  It never
starts OCR, a model, or an external process; raw OCR remains in ``book.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from tools import review_decisions


SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "booktext.canonical_document"
BLOCK_TYPES = {"heading", "paragraph", "list", "table", "figure", "caption", "page_break", "unknown"}


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


def _block_text(block):
    value = block.get("html") or block.get("text") or block.get("markdown") or ""
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
    decisions_path = root / "review_decisions.json"
    decisions = _load(decisions_path) if decisions_path.is_file() else review_decisions.empty_decisions(priority_path, book_path)
    final_pages, final_report = _final_pages(book, priority, decisions, book_path, priority_path, decisions_path, booktext_version)
    surya, raw_surya_ref = _surya_pages(root)
    pages = sorted(book.get("pages", []), key=lambda value: value.get("page", 0))
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
    order = 0
    document_id = _stable_id("document", _hash_file(book_path))

    def add(block):
        nonlocal order
        order += 1
        block["reading_order"] = order
        blocks.append(block)

    for record in pages:
        page = record["page"]
        page_ref = {"source_page": page, "prepared_page": _relative_if_file(root, record.get("page_image")), "raw_ocr_ref": raw_surya_ref}
        item_states = review_by_page.get(page, [])
        source_pages.append({**page_ref, "review": {"item_ids": [item_id for item_id, _status in item_states], "status_counts": dict(Counter(status for _item_id, status in item_states))}})
        if record.get("page_image") and page_ref["prepared_page"] is None:
            warnings.append({"page": page, "kind": "missing_prepared_page_asset", "path": record["page_image"]})
        page_blocks = (surya.get(page) or {}).get("blocks")
        has_changed_text = bool(changed_by_page.get(page))
        table_entries = (record.get("tables") or {}).get("tables") or []
        tables_by_index = {value.get("index"): value for value in table_entries if isinstance(value, dict)}
        emitted_tables = set()

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
        if has_changed_text or not isinstance(page_blocks, list):
            text = final_pages.get(page, str(record.get("text", "")))
            if text:
                add({"block_id": _stable_id("block", f"{document_id}:{page}:page-text"), "type": "unknown", "text": text,
                     "provenance": {**page_ref, "raw_page_text_sha256": _hash_bytes(str(record.get("text", "")).encode()), "correction_decision_ids": [did for _item, did in changed_by_page.get(page, []) if did], "fallback_reason": "valid_corrections_require_page_text_projection" if has_changed_text else "layout_blocks_unavailable"}})
        else:
            raw_table_index = 0
            for index, raw in enumerate(sorted((value for value in page_blocks if isinstance(value, dict)), key=lambda value: value.get("reading_order", 0)), start=1):
                label = str(raw.get("label", ""))
                if label.lower() == "table":
                    raw_table_index += 1
                    if raw_table_index in tables_by_index:
                        add_table(tables_by_index[raw_table_index])
                    continue
                if label.lower() == "figure":
                    continue
                text = _block_text(raw)
                if not text:
                    continue
                kind = _type_for_label(label)
                add({"block_id": _stable_id("block", f"{document_id}:{page}:raw:{index}"), "type": kind, "text": text,
                     "level": None if kind == "heading" else None,
                     "provenance": {**page_ref, "layout_label": label, "layout_reading_order": raw.get("reading_order"), "bbox": raw.get("bbox"), "polygon": raw.get("polygon"), "confidence": raw.get("confidence"), "raw_ocr_ref": raw_surya_ref}})
        for table in sorted(tables_by_index.values(), key=lambda value: value.get("index", 0)):
            add_table(table)
        for visual in record.get("visuals") or []:
            image = _relative_if_file(root, visual.get("image"))
            if image is None:
                warnings.append({"page": page, "kind": "missing_visual_asset", "path": visual.get("image")})
                continue
            add({"block_id": _stable_id("figure", f"{document_id}:{page}:{visual.get('index')}"), "type": "figure", "image": image,
                 "description": visual.get("description"), "description_ref": _relative_if_file(root, visual.get("description_file")), "caption_id": None,
                 "provenance": {**page_ref, "visual_index": visual.get("index"), "visual_type": visual.get("type"), "bbox": visual.get("bbox")}})
        add({"block_id": _stable_id("break", f"{document_id}:{page}"), "type": "page_break", "provenance": page_ref})
    unresolved = final_report.get("unresolved_priority_counts", {})
    source_document = book.get("document") if isinstance(book.get("document"), dict) else {}
    source_files = [Path(value).name for value in book.get("sources", []) if isinstance(value, str)]
    document_title = source_document.get("title") or (Path(source_files[0]).stem if source_files else None)
    document = {
        "schema_version": SCHEMA_VERSION, "artifact_type": ARTIFACT_TYPE, "document_id": document_id,
        "metadata": {"title": document_title, "author": None, "booktext_version": booktext_version, "source_page_count": len(pages), "languages": book.get("ocr_languages"), "ocr_engine": book.get("ocr_engine"), "source_files": source_files, "build_policy": "existing artifacts only; no OCR or model inference"},
        "source": {"book_json": {"path": "book.json", "sha256": _hash_file(book_path)}, "document_identity": {"path": source_document.get("identity_artifact"), "title": document_title}, "review_priority": {"path": "review_priority.json", "sha256": _hash_file(priority_path)}, "review_decisions": {"path": decisions_path.name, "sha256": _hash_file(decisions_path) if decisions_path.is_file() else None}},
        "review": {"final_build": {"decision_status_counts": final_report.get("decision_status_counts", {}), "applied_text_changes": final_report.get("applied_text_changes", 0), "stale_or_conflict_count": final_report.get("stale_or_conflict_count", 0)}, "unresolved_priority_counts": {"HIGH": unresolved.get("HIGH", 0), "MEDIUM": unresolved.get("MEDIUM", 0)}},
        "source_pages": source_pages, "blocks": blocks, "notes": [], "warnings": warnings,
    }
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
            else:
                raise ValueError("unsupported table status")
        for decision_id in provenance.get("correction_decision_ids", []):
            if decision_id not in decision_ids:
                raise ValueError("decision provenance points to nonexistent decision")
        for key in ("prepared_page", "raw_ocr_ref"):
            value = provenance.get(key)
            if value and root is not None and not (Path(root) / value).is_file():
                raise ValueError("broken artifact reference")
        if block.get("type") == "figure" and root is not None and not (Path(root) / block.get("image", "")).is_file():
            raise ValueError("broken figure artifact reference")
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
