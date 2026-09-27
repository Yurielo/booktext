#!/usr/bin/env python3
"""Extract existing text without OCR and report when OCR is required."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


NATIVE_FORMATS = {".txt", ".md", ".markdown"}
TEXTUTIL_FORMATS = {".rtf", ".docx"}


def parse_page_spec(spec: str | None) -> set[int] | None:
    if not spec:
        return None
    pages = set()
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = (int(value) for value in part.split("-", 1))
                if start < 1 or end < start:
                    raise ValueError
                pages.update(range(start, end + 1))
            else:
                page = int(part)
                if page < 1:
                    raise ValueError
                pages.add(page)
        except ValueError as error:
            raise ValueError(f"invalid page selection: {part}") from error
    return pages


def choose_pages(total_pages: int, include_spec: str | None, exclude_spec: str | None) -> list[int]:
    include = parse_page_spec(include_spec)
    exclude = parse_page_spec(exclude_spec) or set()
    selected = include if include is not None else set(range(1, total_pages + 1))
    invalid = sorted(page for page in selected | exclude if page > total_pages)
    if invalid:
        raise ValueError(f"source has {total_pages} pages; unavailable pages: {invalid}")
    selected -= exclude
    if not selected:
        raise ValueError("no pages remain after page selection")
    return sorted(selected)


def textutil_text(source: Path) -> str:
    result = subprocess.run(
        ["textutil", "-convert", "txt", "-stdout", str(source)],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def pdf_page_count(source: Path) -> int:
    info = subprocess.run(
        ["pdfinfo", str(source)], check=True, text=True, capture_output=True
    ).stdout
    return next(
        int(line.split(":", 1)[1].strip())
        for line in info.splitlines()
        if line.startswith("Pages:")
    )


def pdftotext_page(source: Path, page: int) -> str:
    return subprocess.run(
        ["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(source), "-"],
        check=True, text=True, capture_output=True,
    ).stdout


def djvu_page_count(source: Path) -> int:
    return int(subprocess.run(
        ["djvused", str(source), "-e", "n"], check=True, text=True, capture_output=True
    ).stdout.strip())


def djvutxt_page(source: Path, page: int) -> str:
    return subprocess.run(
        ["djvutxt", f"-page={page}", str(source)],
        check=True, text=True, capture_output=True,
    ).stdout


def extract(source: Path, pages: str | None = None, exclude_pages: str | None = None) -> dict:
    suffix = source.suffix.lower()
    if suffix in NATIVE_FORMATS:
        total_pages = 1
        selected = choose_pages(total_pages, pages, exclude_pages)
        page_texts = [(1, source.read_text(encoding="utf-8"))] if selected else []
    elif suffix in TEXTUTIL_FORMATS:
        total_pages = 1
        selected = choose_pages(total_pages, pages, exclude_pages)
        page_texts = [(1, textutil_text(source))] if selected else []
    elif suffix == ".pdf":
        total_pages = pdf_page_count(source)
        selected = choose_pages(total_pages, pages, exclude_pages)
        page_texts = [(page, pdftotext_page(source, page)) for page in selected]
    elif suffix in {".djvu", ".djv"}:
        total_pages = djvu_page_count(source)
        selected = choose_pages(total_pages, pages, exclude_pages)
        page_texts = [(page, djvutxt_page(source, page)) for page in selected]
    else:
        return {
            "artifact_type": "booktext.native_text_extraction",
            "status": "OCR_REQUIRED",
            "reason": f"native extraction is unsupported for {suffix or 'this file'}",
            "pages": [],
        }
    pages = [
        {"page_number": page, "text": text, "status": "NATIVE_TEXT" if text.strip() else "OCR_REQUIRED"}
        for page, text in page_texts
    ]
    return {
        "artifact_type": "booktext.native_text_extraction",
        "status": "COMPLETE" if all(page["status"] == "NATIVE_TEXT" for page in pages) else "PARTIAL_OCR_REQUIRED",
        "total_source_pages": total_pages,
        "selected_source_pages": selected,
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pages", help='pages to extract, e.g. "5,7,11" or "5-11"')
    parser.add_argument("--exclude-pages", help='pages to omit, e.g. "5,7,11" or "5-11"')
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error(f"source not found: {args.source}")
    try:
        result = extract(args.source, args.pages, args.exclude_pages)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
