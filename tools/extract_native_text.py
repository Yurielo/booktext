#!/usr/bin/env python3
"""Extract existing text without OCR and report when OCR is required."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


NATIVE_FORMATS = {".txt", ".md", ".markdown"}
TEXTUTIL_FORMATS = {".rtf", ".docx"}


def textutil_text(source: Path) -> str:
    result = subprocess.run(
        ["textutil", "-convert", "txt", "-stdout", str(source)],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def pdftotext_pages(source: Path) -> list[str]:
    info = subprocess.run(
        ["pdfinfo", str(source)], check=True, text=True, capture_output=True
    ).stdout
    page_count = next(
        int(line.split(":", 1)[1].strip())
        for line in info.splitlines()
        if line.startswith("Pages:")
    )
    return [
        subprocess.run(
            ["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(source), "-"],
            check=True, text=True, capture_output=True,
        ).stdout
        for page in range(1, page_count + 1)
    ]


def extract(source: Path) -> dict:
    suffix = source.suffix.lower()
    if suffix in NATIVE_FORMATS:
        page_texts = [source.read_text(encoding="utf-8")]
    elif suffix in TEXTUTIL_FORMATS:
        page_texts = [textutil_text(source)]
    elif suffix == ".pdf":
        page_texts = pdftotext_pages(source)
    else:
        return {
            "artifact_type": "booktext.native_text_extraction",
            "status": "OCR_REQUIRED",
            "reason": f"native extraction is unsupported for {suffix or 'this file'}",
            "pages": [],
        }
    pages = [
        {"page_number": index, "text": text, "status": "NATIVE_TEXT" if text.strip() else "OCR_REQUIRED"}
        for index, text in enumerate(page_texts, start=1)
    ]
    return {
        "artifact_type": "booktext.native_text_extraction",
        "status": "COMPLETE" if all(page["status"] == "NATIVE_TEXT" for page in pages) else "PARTIAL_OCR_REQUIRED",
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error(f"source not found: {args.source}")
    args.output.write_text(json.dumps(extract(args.source), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
