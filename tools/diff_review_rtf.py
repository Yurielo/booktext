#!/usr/bin/env python3
"""Create non-destructive candidate edits from a reviewed Booktext RTF."""
from __future__ import annotations

import argparse
import difflib
import subprocess
from pathlib import Path


def rtf_text(path: Path) -> str:
    return subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], check=True, text=True, capture_output=True).stdout


def pages(text: str) -> dict[int, str]:
    result, current = {}, None
    for line in text.splitlines():
        if line.startswith("PAGE ") and line[5:].isdigit():
            current = int(line[5:])
            result[current] = ""
        elif current is not None:
            result[current] += line + "\n"
    return {key: value.strip() for key, value in result.items()}


def candidates(baseline: str, reviewed: str) -> dict:
    before, after = pages(baseline), pages(reviewed)
    changes = []
    for page in sorted(before.keys() | after.keys()):
        a, b = before.get(page, "").split(), after.get(page, "").split()
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag != "equal":
                changes.append({"page": page, "status": "PENDING_HUMAN_CONFIRMATION", "kind": tag, "baseline": " ".join(a[i1:i2]), "reviewed": " ".join(b[j1:j2])})
    return {"artifact_type": "booktext.review_edit_candidates", "policy": "No candidate changes OCR or document.json until explicit human confirmation.", "changes": changes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--reviewed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    import json
    args.output.write_text(json.dumps(candidates(rtf_text(args.baseline), rtf_text(args.reviewed)), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
