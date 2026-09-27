#!/usr/bin/env python3
"""Create a human-review RTF from Booktext OCR and its review queue.

Yellow text is an unresolved OCR disagreement, never an automatic correction.
The raw TXT and review JSON remain the authoritative machine artefacts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def rtf_escape(value: str) -> str:
    chunks = []
    for char in value:
        if char in "\\{}":
            chunks.append("\\" + char)
        elif char == "\n":
            chunks.append("\\par\n")
        elif ord(char) < 128:
            chunks.append(char)
        else:
            codepoint = ord(char)
            chunks.append(f"\\u{codepoint if codepoint < 32768 else codepoint - 65536}?")
    return "".join(chunks)


def find_spans(text: str, candidates: list[str]) -> tuple[list[tuple[int, int]], list[str]]:
    spans, unresolved = [], []
    cursor = 0
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        start = text.find(candidate, cursor)
        if start < 0:
            start = text.find(candidate)
        if start < 0:
            unresolved.append(candidate)
            continue
        spans.append((start, start + len(candidate)))
        cursor = start + len(candidate)
    return sorted(spans), unresolved


def highlighted_rtf(text: str, spans: list[tuple[int, int]]) -> str:
    output, cursor = [], 0
    for start, end in spans:
        if start < cursor:
            continue
        output.append(rtf_escape(text[cursor:start]))
        # ``\\highlight`` alone is ignored by some macOS RTF readers.  ``\\cb``
        # is the broadly supported character-background control; retain both
        # so Word and TextEdit render the same yellow review marker.
        output.append("{\\highlight1\\cb1 " + rtf_escape(text[start:end]) + "\\highlight0\\cb0}")
        cursor = end
    output.append(rtf_escape(text[cursor:]))
    return "".join(output)


def build_rtf(book: dict, review: dict) -> tuple[str, dict]:
    by_page = {}
    for item in review.get("review", []):
        if item.get("kind") in {"replace", "delete"} and item.get("surya"):
            by_page.setdefault(item.get("page"), []).append(item["surya"])
    body = [
        r"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}{\colortbl ;\red255\green255\blue0;}\fs22",
        r"\b BOOKTEXT OCR HUMAN REVIEW\b0\par ",
        "Yellow text is an unresolved difference between OCR engines. "
        "Check it against the source image; edit only when the image supports the change. "
        r"The raw TXT remains unchanged.\par\par ",
    ]
    report = {"artifact_type": "booktext.review_rtf_report", "pages": []}
    for page in book.get("pages", []):
        number, text = page.get("page"), str(page.get("text", ""))
        spans, unresolved = find_spans(text, by_page.get(number, []))
        body.append(rf"\par\b PAGE {number}\b0\par " + highlighted_rtf(text, spans) + r"\par")
        report["pages"].append({"page": number, "highlighted_spans": len(spans), "unlocated_review_items": unresolved})
    body.append("}")
    return "".join(body), report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-json", required=True, type=Path)
    parser.add_argument("--review-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    rtf, report = build_rtf(json.loads(args.book_json.read_text(encoding="utf-8")), json.loads(args.review_json.read_text(encoding="utf-8")))
    args.output.write_text(rtf, encoding="ascii")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
