#!/usr/bin/env python3
"""Record human OCR decisions and rebuild a final book without OCR/model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path


SCHEMA_VERSION = "1.0"
DECISIONS_TYPE = "booktext.review_decisions"
FINAL_BUILD_TYPE = "booktext.final_build"
TEXT_ACTIONS = {"keep_surya", "accept_vision", "manual", "ignore", "defer"}
TABLE_ACTIONS = {"ignore", "defer"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    return _sha256_bytes(Path(path).read_bytes())


def item_fingerprint(item):
    return _sha256_bytes(_canonical(item).encode("utf-8"))


def page_fingerprint(page):
    return _sha256_bytes(str(page.get("text", "")).encode("utf-8"))


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON {path}: {error}") from error


def _priority_index(priority):
    if priority.get("artifact_type") != "booktext.review_priority" or priority.get("schema_version") != "1.0":
        raise ValueError("unsupported review priority artifact")
    items = priority.get("items")
    if not isinstance(items, list):
        raise ValueError("review priority items must be a list")
    index = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("item_id"), str):
            raise ValueError("review priority contains an invalid item")
        if item["item_id"] in index:
            raise ValueError(f"duplicate review item ID: {item['item_id']}")
        index[item["item_id"]] = item
    return index


def _page_index(book):
    pages = book.get("pages")
    if not isinstance(pages, list):
        raise ValueError("book manifest pages must be a list")
    index = {}
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("page"), int):
            raise ValueError("book manifest contains an invalid page")
        if page["page"] in index:
            raise ValueError(f"duplicate book page: {page['page']}")
        index[page["page"]] = page
    return index


def empty_decisions(priority_path, book_path):
    return {
        "schema_version": "1.0",
        "artifact_type": DECISIONS_TYPE,
        "priority_source": {"filename": Path(priority_path).name, "sha256": sha256_file(priority_path)},
        "book_source": {"filename": Path(book_path).name, "sha256": sha256_file(book_path)},
        "decisions": [],
    }


def _validate_decision_shape(decision):
    required = {"item_id", "page", "action", "source_anchor"}
    missing = required - decision.keys()
    if missing:
        raise ValueError(f"decision missing fields: {', '.join(sorted(missing))}")
    if decision["action"] not in TEXT_ACTIONS:
        raise ValueError(f"unsupported decision action: {decision['action']}")
    if not isinstance(decision["page"], int):
        raise ValueError("decision page must be an integer")
    if not isinstance(decision["source_anchor"], dict):
        raise ValueError("decision source_anchor must be an object")


def validate_decision(decision, priority_items, pages):
    """Return ``(status, item, page)`` without ever changing source evidence."""
    try:
        _validate_decision_shape(decision)
    except ValueError as error:
        return "invalid", str(error), None
    item = priority_items.get(decision["item_id"])
    if item is None:
        return "stale", "unknown_review_item", None
    if decision["page"] != item.get("page"):
        return "conflict", "page_does_not_match_review_item", None
    page = pages.get(decision["page"])
    if page is None:
        return "stale", "book_page_missing", None
    anchor = decision["source_anchor"]
    if anchor.get("priority_item_sha256") != item_fingerprint(item):
        return "stale", "priority_evidence_changed", None
    if anchor.get("page_text_sha256") != page_fingerprint(page):
        return "stale", "authoritative_raw_page_changed", None
    action = decision["action"]
    if item.get("item_type") == "table_fallback_review":
        if action not in TABLE_ACTIONS:
            return "invalid", "table_fallback_allows_only_ignore_or_defer", None
        return ("deferred" if action == "defer" else "resolved_no_change"), item, page
    if item.get("item_type") != "ocr_qa":
        return "invalid", "unsupported_review_item_type", None
    if action == "accept_vision" and not str(item.get("vision", "")).strip():
        return "invalid", "accept_vision_requires_nonempty_vision_evidence", None
    if action == "manual" and not isinstance(decision.get("replacement_text"), str):
        return "invalid", "manual_requires_explicit_replacement_text", None
    if action == "manual" and not decision["replacement_text"].strip():
        return "invalid", "manual_replacement_text_must_not_be_empty", None
    if action == "defer":
        return "deferred", item, page
    if action in {"keep_surya", "ignore"}:
        return "resolved_no_change", item, page
    return "pending_change", item, page


def _source_pattern(value):
    compact = str(value or "").strip()
    if not compact:
        return None
    return re.compile(re.escape(compact).replace(r"\ ", r"\s+"))


def _find_change_span(page, item):
    pattern = _source_pattern(item.get("surya"))
    if pattern is None:
        return None, "missing_surya_source_anchor"
    matches = list(pattern.finditer(str(page.get("text", ""))))
    if len(matches) == 0:
        return None, "surya_source_text_not_found"
    if len(matches) > 1:
        return None, "surya_source_text_ambiguous"
    return (matches[0].start(), matches[0].end()), None


def _replacement(decision, item):
    return item["vision"] if decision["action"] == "accept_vision" else decision["replacement_text"]


def _decision_statuses(decisions, priority_items, pages):
    statuses = []
    item_counts = Counter(
        decision.get("item_id")
        for decision in decisions
        if isinstance(decision, dict)
    )
    for decision in decisions:
        item_id = decision.get("item_id") if isinstance(decision, dict) else None
        if item_counts[item_id] > 1:
            statuses.append({"decision": decision, "status": "conflict", "reason": "duplicate_decision_for_item"})
            continue
        status, item_or_reason, page = validate_decision(decision, priority_items, pages)
        if status in {"invalid", "stale", "conflict"}:
            statuses.append({"decision": decision, "status": status, "reason": item_or_reason})
        else:
            statuses.append({"decision": decision, "status": status, "item": item_or_reason, "page_record": page})
    return statuses


def build_final(book, priority, decisions_document, *, book_path, priority_path, decisions_path, booktext_version):
    """Deterministically rebuild from raw BookText output and decisions only."""
    if decisions_document.get("artifact_type") != DECISIONS_TYPE or decisions_document.get("schema_version") != "1.0":
        raise ValueError("unsupported review decisions artifact")
    priority_items = _priority_index(priority)
    pages = _page_index(book)
    decisions = decisions_document.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("review decisions must be a list")
    expected_priority = decisions_document.get("priority_source", {}).get("sha256")
    expected_book = decisions_document.get("book_source", {}).get("sha256")
    source_identity_matches = (
        expected_priority == sha256_file(priority_path)
        and expected_book == sha256_file(book_path)
    )
    if source_identity_matches:
        statuses = _decision_statuses(decisions, priority_items, pages)
    else:
        statuses = [
            {"decision": decision, "status": "stale", "reason": "source_output_identity_changed"}
            for decision in decisions
        ]
    changes = []
    for entry in statuses:
        if entry["status"] != "pending_change":
            continue
        span, reason = _find_change_span(entry["page_record"], entry["item"])
        if reason:
            entry.update({"status": "stale", "reason": reason})
            continue
        entry["span"] = span
        entry["replacement"] = _replacement(entry["decision"], entry["item"])
        changes.append(entry)

    for page_number in {entry["page_record"]["page"] for entry in changes}:
        local = [entry for entry in changes if entry["page_record"]["page"] == page_number]
        for index, left in enumerate(local):
            for right in local[index + 1:]:
                if max(left["span"][0], right["span"][0]) < min(left["span"][1], right["span"][1]):
                    left.update({"status": "conflict", "reason": "overlapping_source_text"})
                    right.update({"status": "conflict", "reason": "overlapping_source_text"})

    changed_text = {number: str(page.get("text", "")) for number, page in pages.items()}
    for page_number, raw_text in changed_text.items():
        local = [entry for entry in changes if entry["page_record"]["page"] == page_number and entry["status"] == "pending_change"]
        for entry in sorted(local, key=lambda value: value["span"][0], reverse=True):
            start, end = entry["span"]
            changed_text[page_number] = changed_text[page_number][:start] + entry["replacement"] + changed_text[page_number][end:]
            entry["status"] = "resolved_changed"

    counts = Counter(entry["status"] for entry in statuses)
    unresolved = Counter(
        item["priority"]
        for item in priority_items.values()
        if item["priority"] in {"HIGH", "MEDIUM"}
        and not any(
            entry.get("item", {}).get("item_id") == item["item_id"]
            and entry["status"] in {"resolved_no_change", "resolved_changed"}
            for entry in statuses
        )
    )
    final_text = "\n\n".join(
        f"===== СТРАНИЦА {number} =====\n\n{changed_text[number]}"
        for number in sorted(changed_text)
    ) + "\n"
    final_markdown = render_final_markdown(book, changed_text)
    report = {
        "schema_version": "1.0",
        "artifact_type": FINAL_BUILD_TYPE,
        "booktext_version": booktext_version,
        "source": {
            "book": {"filename": Path(book_path).name, "sha256": sha256_file(book_path)},
            "priority": {"filename": Path(priority_path).name, "sha256": sha256_file(priority_path)},
            "decisions": {"filename": Path(decisions_path).name, "sha256": sha256_file(decisions_path)},
        },
        "decision_status_counts": dict(sorted(counts.items())),
        "unresolved_priority_counts": {"HIGH": unresolved["HIGH"], "MEDIUM": unresolved["MEDIUM"]},
        "applied_text_changes": sum(entry["status"] == "resolved_changed" for entry in statuses),
        "stale_or_conflict_count": sum(entry["status"] in {"stale", "conflict", "invalid"} for entry in statuses),
        "artifacts": {"text": "book_final.txt", "markdown": "book_final.md"},
        "policy": "Built only from authoritative raw BookText OCR and validated human decisions; no OCR or model inference was invoked.",
        "decisions": [
            {"item_id": entry["decision"].get("item_id"), "action": entry["decision"].get("action"), "status": entry["status"], "reason": entry.get("reason")}
            for entry in statuses
        ],
    }
    return final_text, final_markdown, report


def render_final_markdown(book, changed_text):
    """Render from the raw manifest, retaining safe visual/table references."""
    lines = ["# BookText final", "", "Built from raw OCR plus validated human decisions."]
    for record in sorted(book.get("pages", []), key=lambda item: item["page"]):
        lines.extend(["", f"## Страница {record['page']}", "", changed_text[record["page"]] or "_Текст не обнаружен._"])
        for visual in record.get("visuals") or []:
            lines.extend(["", f"![{visual.get('type', 'image')}]({visual.get('image', '')})", "", visual.get("description", "")])
        for table in (record.get("tables") or {}).get("tables", []):
            if table.get("status") == "HTML_TABLE":
                lines.extend(["", f"Structured table: `{table.get('html', '')}`"])
            elif table.get("status") == "fallback_review":
                lines.extend(["", "Table fallback review evidence only; no cells are verified."])
    return "\n".join(lines).rstrip() + "\n"


def write_final(output_dir, final_text, final_markdown, report):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "book_final.txt").write_text(final_text, encoding="utf-8")
    (output_dir / "book_final.md").write_text(final_markdown, encoding="utf-8")
    (output_dir / "final_build.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record_decision(*, decisions_path, priority_path, book_path, item_id, action, replacement_text=None, note=None, replace=False):
    priority = load_json(priority_path)
    book = load_json(book_path)
    items, pages = _priority_index(priority), _page_index(book)
    item = items.get(item_id)
    if item is None:
        raise ValueError(f"unknown review item ID: {item_id}")
    decision = {
        "decision_id": _sha256_bytes(
            _canonical({"item_id": item_id, "action": action, "replacement_text": replacement_text}).encode("utf-8")
        )[:20],
        "item_id": item_id,
        "page": item["page"],
        "action": action,
        "source_anchor": {
            "priority_item_sha256": item_fingerprint(item),
            "page_text_sha256": page_fingerprint(pages[item["page"]]),
        },
    }
    if replacement_text is not None:
        decision["replacement_text"] = replacement_text
    if note:
        decision["note"] = note
    status, reason_or_item, _ = validate_decision(decision, items, pages)
    if status in {"invalid", "stale", "conflict"}:
        raise ValueError(f"cannot record decision: {reason_or_item}")
    path = Path(decisions_path)
    document = load_json(path) if path.exists() else empty_decisions(priority_path, book_path)
    if document.get("artifact_type") != DECISIONS_TYPE or document.get("schema_version") != "1.0":
        raise ValueError("unsupported review decisions artifact")
    existing = document.setdefault("decisions", [])
    positions = [index for index, entry in enumerate(existing) if entry.get("item_id") == item_id]
    if positions and not replace:
        raise ValueError("decision already exists; pass --replace to update it")
    if len(positions) > 1:
        raise ValueError("conflicting duplicate decisions already exist")
    if positions:
        existing[positions[0]] = decision
    else:
        existing.append(decision)
    existing.sort(key=lambda entry: entry["item_id"])
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return decision


def main():
    parser = argparse.ArgumentParser(description="Record BookText human decisions and rebuild without OCR.")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="List priority items")
    listing.add_argument("--priority", type=Path, required=True)
    listing.add_argument("--page", type=int)
    record = sub.add_parser("record", help="Record one validated human decision")
    record.add_argument("--priority", type=Path, required=True)
    record.add_argument("--book", type=Path, required=True)
    record.add_argument("--decisions", type=Path, required=True)
    record.add_argument("--item", required=True)
    record.add_argument("--action", choices=sorted(TEXT_ACTIONS), required=True)
    record.add_argument("--replacement")
    record.add_argument("--note")
    record.add_argument("--replace", action="store_true")
    build = sub.add_parser("build", help="Build final book from raw OCR and decisions only")
    build.add_argument("--priority", type=Path, required=True)
    build.add_argument("--book", type=Path, required=True)
    build.add_argument("--decisions", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "list":
            for item in _priority_index(load_json(args.priority)).values():
                if args.page is None or item["page"] == args.page:
                    print(f"{item['item_id']} page={item['page']} priority={item['priority']} type={item['item_type']}")
            return
        if args.command == "record":
            decision = record_decision(
                decisions_path=args.decisions, priority_path=args.priority, book_path=args.book,
                item_id=args.item, action=args.action, replacement_text=args.replacement,
                note=args.note, replace=args.replace,
            )
            print(f"Recorded {decision['action']} for {decision['item_id']}")
            return
        final_text, final_markdown, report = build_final(
            load_json(args.book), load_json(args.priority), load_json(args.decisions),
            book_path=args.book, priority_path=args.priority, decisions_path=args.decisions,
            booktext_version="0.1.0",
        )
        write_final(args.output_dir, final_text, final_markdown, report)
        print(json.dumps(report["decision_status_counts"], ensure_ascii=False, sort_keys=True))
        if report["stale_or_conflict_count"]:
            raise SystemExit(2)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
