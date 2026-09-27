#!/usr/bin/env python3
"""Import a user-reviewed RTF as a new text revision without touching raw OCR."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

try:
    from tools.diff_review_rtf import candidates, pages, rtf_text
except ModuleNotFoundError:
    from diff_review_rtf import candidates, pages, rtf_text


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_review(baseline: Path, reviewed: Path, text_output: Path, log_output: Path) -> dict:
    baseline_text, reviewed_text = rtf_text(baseline), rtf_text(reviewed)
    reviewed_pages = pages(reviewed_text)
    text_output.write_text(
        "\n\n".join(f"===== PAGE {number} =====\n\n{text}" for number, text in sorted(reviewed_pages.items())) + "\n",
        encoding="utf-8",
    )
    report = candidates(baseline_text, reviewed_text)
    for change in report["changes"]:
        change["status"] = "ACCEPTED_BY_HUMAN_REVIEW"
    report.update({
        "artifact_type": "booktext.human_review_import",
        "human_review_status": "COMPLETED",
        "reviewer": "unspecified",
        "raw_ocr_modified": False,
        "baseline_rtf_sha256": sha256_file(baseline),
        "reviewed_rtf_sha256": sha256_file(reviewed),
        "human_reviewed_text_sha256": sha256_file(text_output),
    })
    log_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--reviewed", required=True, type=Path)
    parser.add_argument("--text-output", required=True, type=Path)
    parser.add_argument("--log-output", required=True, type=Path)
    args = parser.parse_args()
    if not args.baseline.is_file() or not args.reviewed.is_file():
        parser.error("baseline and reviewed RTF files must exist")
    report = import_review(args.baseline, args.reviewed, args.text_output, args.log_output)
    print(f"imported {len(report['changes'])} human-reviewed changes")


if __name__ == "__main__":
    main()
