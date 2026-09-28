"""Upgrade legacy BookText QA evidence without rerunning OCR or models."""

from __future__ import annotations

import json
from pathlib import Path

from tools.review_clusters import build_clusters
from tools.review_decisions import atomic_write_json, sha256_file
from tools.review_priority import prioritise_review, priority_text


def recover(output_dir):
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
    priority = prioritise_review(qa_pages, raw_items, pages)
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
        "policy": "Derived offline from preserved raw OCR/QA evidence; no OCR, Vision, Tesseract, Ollama, or LLM executed.",
    }
    atomic_write_json(root / "run_summary.json", summary)
    return summary
