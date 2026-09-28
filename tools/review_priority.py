"""Deterministic human-review prioritisation for BookText QA evidence.

This layer never edits OCR text.  It ranks independent QA and table evidence so
that a reviewer can start with plausible data-loss and structural risks.
"""

import hashlib
import json
import re


SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "booktext.review_priority"
PRIORITIES = ("HIGH", "MEDIUM", "LOW", "INFO")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?")
_FRACTION = re.compile(r"\d+\s*/\s*\d+")


def _compact(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalise_segmentation(value):
    return re.sub(r"[\s\-‐‑‒–—]+", "", _compact(value)).casefold()


def _has_only_letters_and_separators(value):
    return bool(re.fullmatch(r"[\s\-‐‑‒–—\w]+", str(value or ""), re.UNICODE)) and not bool(
        re.search(r"\d", str(value or ""))
    )


def _numbers(value):
    return _NUMBER.findall(_compact(value))


def _stable_id(prefix, payload):
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _page_metrics(qa_pages):
    return {
        item.get("page"): item
        for item in qa_pages
        if isinstance(item, dict) and isinstance(item.get("page"), int)
    }


def prioritise_review(qa_pages, review_items, page_records):
    """Return a versioned, deterministic priority artefact.

    Exact duplicate QA records are folded into one item.  The raw review list
    remains untouched; ``duplicate_count`` merely records the safe grouping.
    """
    metrics = _page_metrics(qa_pages)
    grouped = {}

    for raw in review_items:
        if not isinstance(raw, dict) or not isinstance(raw.get("page"), int):
            continue
        stable_payload = {
            "page": raw["page"],
            "kind": raw.get("kind", ""),
            "category": raw.get("category", "text"),
            "surya": _compact(raw.get("surya")),
            "vision": _compact(raw.get("vision")),
            "vision_bbox": raw.get("vision_bbox"),
        }
        key = _stable_id(f"qa-p{raw['page']:04d}", stable_payload)
        grouped.setdefault(key, {"raw": raw, "count": 0})["count"] += 1

    items = []
    for item_id, grouped_item in grouped.items():
        raw = grouped_item["raw"]
        page = raw["page"]
        surya = _compact(raw.get("surya"))
        vision = _compact(raw.get("vision"))
        page_metric = metrics.get(page, {})
        reasons = []
        signals = {
            "qa_kind": raw.get("kind", ""),
            "qa_category": raw.get("category", "text"),
            "vision_confidence": raw.get("vision_confidence"),
            # Coordinates are evidence only: they identify what was seen by
            # an engine on the source page, never text to apply automatically.
            "vision_bbox": raw.get("vision_bbox"),
            "vision_bbox_coordinate_space": raw.get(
                "vision_bbox_coordinate_space", "vision_normalized_bottom_left"
            ),
            "surya_bbox": raw.get("surya_bbox"),
            "surya_bbox_provenance": raw.get("surya_bbox_provenance"),
            "page_agreement": page_metric.get("agreement"),
            "page_word_gap": abs(
                int(page_metric.get("surya_words", 0) or 0)
                - int(page_metric.get("vision_words", 0) or 0)
            ),
        }
        number_mismatch = _numbers(surya) != _numbers(vision)
        fraction_mismatch = bool(_FRACTION.search(surya) or _FRACTION.search(vision)) and number_mismatch
        harmless_segmentation = (
            _normalise_segmentation(surya) == _normalise_segmentation(vision)
            and _normalise_segmentation(surya)
            and _has_only_letters_and_separators(surya)
            and _has_only_letters_and_separators(vision)
        )
        category = raw.get("category", "text")
        kind = str(raw.get("kind", "")).lower()

        if number_mismatch:
            priority = "HIGH"
            reasons.append("numeric_disagreement")
            if fraction_mismatch:
                reasons.append("fraction_or_ratio_disagreement")
        elif category == "completeness" or kind in {"insert", "delete"}:
            priority = "HIGH"
            reasons.append("possible_completeness_or_structure_loss")
        elif harmless_segmentation:
            priority = "LOW"
            reasons.append("harmless_word_segmentation_or_hyphenation")
        elif max(len(surya), len(vision)) <= 2:
            priority = "LOW"
            reasons.append("short_ambiguous_glyph_disagreement")
        else:
            priority = "MEDIUM"
            reasons.append("independent_ocr_text_disagreement")

        if signals["page_word_gap"] >= 25 and priority != "HIGH":
            priority = "HIGH"
            reasons.append("large_page_word_count_gap")
        if signals["page_agreement"] is not None and signals["page_agreement"] <= 0.25 and priority != "HIGH":
            priority = "HIGH"
            reasons.append("very_low_page_level_agreement")
        if grouped_item["count"] > 1:
            reasons.append("exact_duplicate_grouped")

        items.append({
            "item_id": item_id,
            "priority": priority,
            "page": page,
            "item_type": "ocr_qa",
            "reasons": reasons,
            "signals": signals,
            "surya": surya,
            "vision": vision,
            "duplicate_count": grouped_item["count"],
        })

    for record in page_records:
        if not isinstance(record, dict) or not isinstance(record.get("page"), int):
            continue
        for table in (record.get("tables") or {}).get("tables", []):
            if not isinstance(table, dict) or table.get("status") != "fallback_review":
                continue
            page = record["page"]
            index = table.get("index", 0)
            payload = {"page": page, "index": index, "source": table.get("source"), "bbox": table.get("bbox")}
            items.append({
                "item_id": _stable_id(f"table-p{page:04d}-{int(index):02d}", payload),
                "priority": "INFO",
                "page": page,
                "item_type": "table_fallback_review",
                "table_index": index,
                "reasons": ["table_fallback_review_required", "not_verified_structured_data"],
                "signals": {"source": table.get("source"), "diagnostic": table.get("diagnostic"), "confidence": table.get("confidence")},
                "duplicate_count": 1,
            })

    rank = {name: index for index, name in enumerate(PRIORITIES)}
    items.sort(key=lambda item: (rank[item["priority"]], item["page"], item["item_id"]))
    counts = {priority: sum(item["priority"] == priority for item in items) for priority in PRIORITIES}
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "policy": "Surya remains primary OCR. Priority items are human-review evidence only; no OCR text is changed automatically.",
        "counts": counts,
        "items": items,
    }


def priority_text(artifact):
    """Render a compact, stable text view of the priority queue."""
    lines = [
        "BOOKTEXT PRIORITISED REVIEW",
        "",
        "Surya is the primary OCR. This queue is evidence for human review only.",
        "Fallback tables are not verified structured data.",
        "",
        "Counts: " + ", ".join(
            f"{priority}={artifact['counts'].get(priority, 0)}" for priority in PRIORITIES
        ),
    ]
    for item in artifact["items"]:
        lines.extend([
            "",
            f"[{item['priority']}] {item['item_id']} page={item['page']}",
            "Reasons: " + ", ".join(item["reasons"]),
        ])
        if item["item_type"] == "ocr_qa":
            lines.extend([f"SURYA : {item['surya']}", f"VISION: {item['vision']}"])
        else:
            lines.append("TABLE: fallback review evidence only; no cells are verified.")
    if not artifact["items"]:
        lines.append("")
        lines.append("No prioritised review items were produced.")
    return "\n".join(lines).rstrip() + "\n"
