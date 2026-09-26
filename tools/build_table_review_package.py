#!/usr/bin/env python3
"""Build a conservative, review-first package for OCR tables.

The tool never infers a corrected table from different OCR engines. It keeps
Surya's HTML table as the primary artefact and records Apple Vision's structure
as review evidence. A person or a later validated reconciler must accept cells.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

try:  # package import for tests and library use
    from tools.extract_surya_tables import expand_grid, get_page, parse_table
except ModuleNotFoundError:  # direct ``python tools/build_table_review_package.py``
    from extract_surya_tables import expand_grid, get_page, parse_table

SCHEMA_VERSION = "1.0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_descriptor(path: Path) -> dict[str, str]:
    return {"filename": path.name, "sha256": sha256_file(path)}


def _table_evidence(vision: dict[str, Any] | None) -> dict[str, Any]:
    if vision is None:
        return {"available": False, "table_count": 0, "line_count": 0, "tables": [], "lines": []}
    return {
        "available": True,
        "table_count": len(vision.get("tables", [])),
        "line_count": len(vision.get("lines", [])),
        "tables": vision.get("tables", []),
        "lines": vision.get("lines", []),
    }


def table_blocks(page_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Support both legacy ``tables`` and current Surya ``blocks`` output."""
    explicit_tables = page_result.get("tables")
    if isinstance(explicit_tables, list):
        return [table for table in explicit_tables if isinstance(table, dict)]
    return [
        block
        for block in page_result.get("blocks", [])
        if isinstance(block, dict) and str(block.get("label", "")).lower() == "table"
    ]


def build_review_package(*, page: int, results_path: Path, vision_path: Path | None) -> dict[str, Any]:
    """Create one page package without silently accepting OCR reconciliation."""
    results = json.loads(results_path.read_text(encoding="utf-8"))
    page_result = get_page(results, page)
    vision = json.loads(vision_path.read_text(encoding="utf-8")) if vision_path and vision_path.exists() else None
    package: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "booktext.table_review_package",
        "page": page,
        "sources": {
            "surya_results": source_descriptor(results_path),
            "vision": source_descriptor(vision_path) if vision_path and vision_path.exists() else None,
        },
        "review_required": True,
        "review_reasons": ["cross_engine_cell_reconciliation_not_implemented"],
        "vision_evidence": _table_evidence(vision),
        "tables": [],
    }
    if vision is None:
        package["review_reasons"].append("vision_evidence_unavailable")
    if page_result is None:
        package["status"] = "MISSING_SURYA_PAGE"
        package["review_reasons"].append("surya_page_missing")
        return package
    for table_index, raw_table in enumerate(table_blocks(page_result), start=1):
        package["tables"].append(
            {
                "table_index": table_index,
                "source": "surya_html",
                "bbox": raw_table.get("bbox"),
                "confidence": raw_table.get("confidence"),
                "raw_html": raw_table.get("html", ""),
                "grid": expand_grid(parse_table(raw_table.get("html", ""))),
                "review_required": True,
            }
        )
    package["status"] = "REVIEW_REQUIRED" if package["tables"] else "NO_SURYA_TABLES"
    if not package["tables"]:
        package["review_reasons"].append("surya_page_has_no_tables")
    return package


def write_package(package: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"page-{package['page']:04d}-table-review.json"
    output_path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", required=True, type=int)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    results_path = args.data_root / "surya" / "surya-input" / "results.json"
    vision_path = args.data_root / "vision" / f"page-{args.page:04d}.json"
    if not results_path.exists():
        parser.error(f"Surya results not found: {results_path}")
    package = build_review_package(page=args.page, results_path=results_path, vision_path=vision_path if vision_path.exists() else None)
    print(f"wrote {write_package(package, args.output_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
