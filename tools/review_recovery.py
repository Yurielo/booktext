"""Upgrade legacy BookText QA evidence without rerunning OCR or models."""

from __future__ import annotations

import json
import html
import re
from pathlib import Path

from tools.review_clusters import build_clusters
from tools.review_decisions import atomic_write_json, sha256_file
from tools.review_priority import prioritise_review, priority_text


def _compact(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _block_text(block):
    return _compact(re.sub(r"<[^>]+>", " ", html.unescape(str(block.get("html", "")))))


def _normalised_bbox(block, image_bbox):
    """Return a source-block rectangle only when the raw geometry is valid."""
    bbox = block.get("bbox")
    if not (isinstance(bbox, list) and len(bbox) == 4 and isinstance(image_bbox, list) and len(image_bbox) == 4):
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
        ix0, iy0, ix1, iy1 = (float(value) for value in image_bbox)
    except (TypeError, ValueError):
        return None
    width, height = ix1 - ix0, iy1 - iy0
    if width <= 0 or height <= 0 or x1 <= x0 or y1 <= y0:
        return None
    result = [(x0 - ix0) / width, (y0 - iy0) / height, (x1 - ix0) / width, (y1 - iy0) / height]
    return result if all(0 <= value <= 1 for value in result) else None


def _add_surya_block_locations(review_items, results_path):
    """Attach conservative *block* locations from already-preserved Surya output.

    Legacy delete disagreements have no Vision words and therefore no Vision
    rectangle.  We do not rerun an engine or guess a word position: if the
    complete preserved Surya phrase occurs in exactly one source text block,
    its block rectangle is retained as auxiliary review evidence.
    """
    if results_path is None or not results_path.is_file():
        return None
    data = json.loads(results_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Surya results artifact must be a JSON object")
    located = 0
    for item in review_items:
        if not isinstance(item, dict) or item.get("vision_bbox") is not None or item.get("surya_bbox") is not None:
            continue
        phrase = _compact(item.get("surya"))
        page_number = item.get("page")
        if not phrase or not isinstance(page_number, int):
            continue
        records = data.get(f"page-{page_number:04d}")
        if not (isinstance(records, list) and records and isinstance(records[0], dict)):
            continue
        record = records[0]
        matches = [block for block in record.get("blocks", []) if isinstance(block, dict) and phrase in _block_text(block)]
        # More than one matching block is ambiguous.  Keep it unlocated.
        if len(matches) != 1:
            continue
        bbox = _normalised_bbox(matches[0], record.get("image_bbox"))
        if bbox is None:
            continue
        item["surya_bbox"] = bbox
        item["surya_bbox_provenance"] = "preserved_surya_text_block"
        located += 1
    return {"path": str(results_path), "located_item_count": located}


def _find_surya_results(root, explicit_path):
    if explicit_path is not None:
        path = Path(explicit_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError("specified Surya results artifact does not exist")
        return path
    for relative in ("surya/surya-input/results.json", "surya/results.json"):
        path = root / relative
        if path.is_file():
            return path
    return None


def recover(output_dir, *, surya_results=None):
    root = Path(output_dir).expanduser().resolve()
    book_path, review_path = root / "book.json", root / "review.json"
    if not book_path.is_file() or not review_path.is_file():
        raise ValueError("recover requires existing book.json and review.json")
    book = json.loads(book_path.read_text(encoding="utf-8"))
    review = json.loads(review_path.read_text(encoding="utf-8"))
    pages, qa_pages, raw_items = book.get("pages"), review.get("pages"), review.get("review")
    if not isinstance(pages, list) or not isinstance(qa_pages, list) or not isinstance(raw_items, list):
        raise ValueError("legacy artifacts do not contain pages/review evidence")
    if len(pages) != len(qa_pages):
        raise ValueError("book and QA page counts differ; refusing recovery")
    # Work on copies: the recovered review layer must not mutate review.json.
    enriched_items = [dict(item) if isinstance(item, dict) else item for item in raw_items]
    results_path = _find_surya_results(root, surya_results)
    locator = _add_surya_block_locations(enriched_items, results_path)
    priority = prioritise_review(qa_pages, enriched_items, pages)
    clusters = build_clusters(priority)
    atomic_write_json(root / "review_priority.json", priority)
    (root / "review_priority.txt").write_text(priority_text(priority), encoding="utf-8")
    atomic_write_json(root / "review_clusters.json", clusters)
    unavailable = [{
        "page": 319, "status": "EXTERNAL_EVIDENCE_UNAVAILABLE",
        "recovery": "DEFERRED_RECOVERY",
        "artifact_paths": [
            "priority_A_review/vision_table_fallback/page-0319-manifest.json",
            "priority_A_review/vision_table_fallback/page-0319-fallback-01.json",
        ],
        "reason": "preserved structural fallback was not locally materialized; no synthetic item, cells, rows, or text were created",
    }]
    summary = {
        "schema_version": "1.0", "artifact_type": "booktext.run_summary",
        "recovered_offline": True,
        "source": {"book_sha256": sha256_file(book_path), "review_sha256": sha256_file(review_path)},
        "pages": {"total": len(pages), "processed": len(pages), "failed": 0},
        "raw_review_difference_count": len(raw_items),
        "review_priority": priority["counts"],
        "review_clusters": {"cluster_count": clusters["cluster_count"], **clusters["metrics"]},
        "external_evidence_unavailable": unavailable,
        "source_location_evidence": locator,
        "policy": "Derived offline from preserved raw OCR/QA evidence; no OCR, Vision, Tesseract, Ollama, or LLM executed.",
    }
    atomic_write_json(root / "run_summary.json", summary)
    return summary
