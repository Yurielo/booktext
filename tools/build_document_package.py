#!/usr/bin/env python3
"""Create Booktext's portable, provenance-preserving document package.

This is an internal Booktext format, not an Integra Knowledge import. It has no
semantic claims and deliberately marks every machine-derived block for review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

try:
    from booktext import _surya_html_to_text
    from tools.extract_surya_tables import expand_grid, get_page, parse_table
except ModuleNotFoundError:  # direct execution from the tools directory
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from booktext import _surya_html_to_text
    from extract_surya_tables import expand_grid, get_page, parse_table


SCHEMA_VERSION = "0.1"
NAMESPACE = uuid.UUID("ef64440e-692e-4527-b3f4-e4ee7dc7f68c")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(kind: str, value: str) -> str:
    return f"{kind}-{uuid.uuid5(NAMESPACE, value)}"


def table_blocks(page_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(page_result, dict):
        return []
    tables = page_result.get("tables")
    if isinstance(tables, list):
        return [table for table in tables if isinstance(table, dict)]
    return [
        block for block in page_result.get("blocks", [])
        if isinstance(block, dict) and str(block.get("label", "")).lower() == "table"
    ]


def _region(block: dict[str, Any]) -> dict[str, Any] | None:
    bbox = block.get("bbox")
    polygon = block.get("polygon")
    if not isinstance(bbox, list) and not isinstance(polygon, list):
        return None
    return {
        "coordinate_space": "prepared_page_pixels",
        "orientation": "up",
        "bbox": bbox if isinstance(bbox, list) else None,
        "polygon": polygon if isinstance(polygon, list) else None,
    }


def _surya_blocks(page_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(page_result, dict):
        return []
    blocks = page_result.get("blocks")
    return [block for block in blocks if isinstance(block, dict)] if isinstance(blocks, list) else []


def _canonical_path(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve())


def _build_sources(
    book: dict[str, Any], source_paths: list[Path]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    declared = [_canonical_path(path) for path in book.get("sources", [])]
    sources = []
    source_by_path = {}
    for input_index, path in enumerate(source_paths, start=1):
        canonical_path = _canonical_path(path)
        source_index = (
            declared.index(canonical_path) + 1
            if canonical_path in declared
            else input_index
        )
        digest = sha256_file(path)
        source = {
            "source_id": stable_id("source", digest),
            "source_index": source_index,
            "filename": path.name,
            "sha256": digest,
        }
        sources.append(source)
        source_by_path[canonical_path] = source
    return sources, source_by_path


def _source_for_page(
    book: dict[str, Any],
    source_by_path: dict[str, dict[str, Any]],
    sources: list[dict[str, Any]],
    page_number: int,
    page_index: int,
) -> tuple[dict[str, Any], int]:
    declared = [_canonical_path(path) for path in book.get("sources", [])]
    if len(declared) > 1:
        if page_number > len(declared):
            raise ValueError(f"page {page_number} has no declared source image")
        source = source_by_path.get(declared[page_number - 1])
        if source is None:
            raise ValueError(f"source for physical page {page_number} was not supplied")
        return source, 1
    if len(sources) == 1:
        return sources[0], page_number
    if page_index > len(sources):
        raise ValueError(f"page {page_number} has no supplied source")
    return sources[page_index - 1], 1


def make_document(
    *, book: dict[str, Any], source_paths: list[Path], book_sha256: str,
    surya: dict[str, Any] | None, review: dict[str, Any] | None,
) -> dict[str, Any]:
    sources, source_by_path = _build_sources(book, source_paths)
    document_id = stable_id("document", ":".join(item["sha256"] for item in sources))
    revision_id = stable_id("revision", f"{document_id}:{book_sha256}")
    pages = []
    for page_index, page in enumerate(book.get("pages", []), start=1):
        page_number = int(page.get("page", page_index))
        source, source_unit_index = _source_for_page(
            book, source_by_path, sources, page_number, page_index
        )
        page_id = stable_id("page", f"{revision_id}:{page_number}")
        surya_page = get_page(surya, page_number) if surya else None
        blocks = []
        for block_index, block in enumerate(
            sorted(_surya_blocks(surya_page), key=lambda value: value.get("reading_order", 0)), start=1
        ):
            raw_html = str(block.get("html", ""))
            block_id = stable_id("block", f"{page_id}:{block_index}")
            entry: dict[str, Any] = {
                "block_id": block_id,
                "kind": str(block.get("label", "unknown")).lower(),
                "reading_order": block.get("reading_order", block_index - 1),
                "region": _region(block),
                "raw_ocr": {"engine": "surya", "html": raw_html},
                "chosen_text": _surya_html_to_text(raw_html),
                "confidence": block.get("confidence"),
                "review_state": "REVIEW_REQUIRED",
                "review_history": [],
            }
            if entry["kind"] == "table":
                grid = expand_grid(parse_table(raw_html))
                entry["cells"] = [
                    {
                        "cell_id": stable_id("cell", f"{block_id}:{row_index}:{column_index}"),
                        "row_index": row_index,
                        "column_index": column_index,
                        "raw_text": value,
                        "chosen_text": value,
                        "review_state": "REVIEW_REQUIRED",
                    }
                    for row_index, row in enumerate(grid)
                    for column_index, value in enumerate(row)
                ]
            blocks.append(entry)
        if not blocks:
            blocks.append({
                "block_id": stable_id("block", f"{page_id}:fallback"),
                "kind": "page_text",
                "reading_order": 0,
                "region": None,
                "raw_ocr": None,
                "chosen_text": str(page.get("text", "")),
                "confidence": None,
                "review_state": "REVIEW_REQUIRED",
                "review_history": [],
                "limitations": ["block_coordinates_and_raw_ocr_unavailable"],
            })
        pages.append({
            "page_id": page_id,
            "page_number": page_number,
            "source_id": source["source_id"],
            "source_locator": {
                "source_index": source["source_index"],
                "source_unit_index": source_unit_index,
                "physical_page_number": page_number,
            },
            "prepared_page_asset": page.get("page_image"),
            "blocks": blocks,
        })
    review_items = (review or {}).get("review", [])
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "booktext.document",
        "document_id": document_id,
        "revision_id": revision_id,
        "parent_revision_id": None,
        "sources": sources,
        "ocr": {"engine": book.get("ocr_engine"), "languages": book.get("ocr_languages")},
        "assurance": {"machine_status": "REVIEW_REQUIRED", "human_verification": "NOT_VERIFIED"},
        "pages": pages,
        "review_items": [
            {"review_id": stable_id("review", f"{revision_id}:{index}"), "status": "OPEN", "evidence": item}
            for index, item in enumerate(review_items, start=1)
        ],
    }


def validate_document(document: dict[str, Any]) -> None:
    required = {"schema_version", "artifact_type", "document_id", "revision_id", "sources", "pages"}
    missing = required - document.keys()
    if missing or document.get("artifact_type") != "booktext.document":
        raise ValueError(f"invalid document header; missing={sorted(missing)}")
    source_ids = {source.get("source_id") for source in document["sources"]}
    if not source_ids or any(not isinstance(item, str) for item in source_ids):
        raise ValueError("document needs stable source IDs")
    seen = set()
    for page in document["pages"]:
        if page.get("page_id") in seen or page.get("source_id") not in source_ids:
            raise ValueError("page has duplicate ID or unknown source")
        seen.add(page["page_id"])
        for block in page.get("blocks", []):
            if not block.get("block_id") or "chosen_text" not in block or "review_history" not in block:
                raise ValueError("block lacks provenance fields")
            for cell in block.get("cells", []):
                if not cell.get("cell_id") or "chosen_text" not in cell:
                    raise ValueError("table cell lacks identifier or text")


def write_package(document: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    document_path = output_dir / "document.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "booktext.package_manifest",
        "document_id": document["document_id"],
        "revision_id": document["revision_id"],
        "package_status": "REVIEW_REQUIRED",
        "files": [{"filename": "document.json", "sha256": sha256_file(document_path)}],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-json", required=True, type=Path)
    parser.add_argument("--source", required=True, action="append", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--surya-results", type=Path)
    parser.add_argument("--review-json", type=Path)
    args = parser.parse_args()
    if any(not path.is_file() for path in [args.book_json, *args.source]):
        parser.error("book-json and every source must be existing files")
    for optional_path in [args.surya_results, args.review_json]:
        if optional_path is not None and not optional_path.is_file():
            parser.error(f"file not found: {optional_path}")
    book = json.loads(args.book_json.read_text(encoding="utf-8"))
    document = make_document(
        book=book, source_paths=args.source, book_sha256=sha256_file(args.book_json),
        surya=json.loads(args.surya_results.read_text(encoding="utf-8")) if args.surya_results else None,
        review=json.loads(args.review_json.read_text(encoding="utf-8")) if args.review_json else None,
    )
    validate_document(document)
    write_package(document, args.output_dir)
    print(f"wrote {args.output_dir / 'document.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
