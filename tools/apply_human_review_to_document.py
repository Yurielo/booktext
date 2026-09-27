#!/usr/bin/env python3
"""Create a reviewed document revision from an accepted Booktext RTF import."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

try:
    from tools.build_document_package import stable_id, validate_document
    from tools.diff_review_rtf import pages
except ModuleNotFoundError:
    from build_document_package import stable_id, validate_document
    from diff_review_rtf import pages


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_review(document: dict, reviewed_text: str, review_import: dict, reviewed_sha256: str) -> dict:
    validate_document(document)
    if review_import.get("human_review_status") != "COMPLETED":
        raise ValueError("human review import must have COMPLETED status")
    reviewed_pages = pages(reviewed_text)
    document_pages = {page["page_number"]: page for page in document["pages"]}
    if set(reviewed_pages) != set(document_pages):
        raise ValueError("reviewed text pages do not exactly match document pages")

    revised = copy.deepcopy(document)
    parent_revision_id = document["revision_id"]
    revised["parent_revision_id"] = parent_revision_id
    revised["revision_id"] = stable_id("revision", f"{parent_revision_id}:{reviewed_sha256}")
    changes_by_page = {}
    for change_index, change in enumerate(review_import.get("changes", []), start=1):
        if change.get("status") != "ACCEPTED_BY_HUMAN_REVIEW":
            raise ValueError("review import contains unaccepted change")
        changes_by_page.setdefault(change.get("page"), []).append({
            "review_change_id": stable_id("review-change", f"{revised['revision_id']}:{change_index}"),
            "baseline": change.get("baseline", ""),
            "reviewed": change.get("reviewed", ""),
            "kind": change.get("kind"),
            "status": change["status"],
        })
    for page in revised["pages"]:
        page_number = page["page_number"]
        page["human_review"] = {
            "status": "HUMAN_REVIEWED",
            "reviewed_text": reviewed_pages[page_number],
            "changes": changes_by_page.get(page_number, []),
        }
    revised["assurance"] = {
        "machine_status": document.get("assurance", {}).get("machine_status", "REVIEW_REQUIRED"),
        "human_verification": "PAGE_TEXT_REVIEWED",
        "reviewed_text_sha256": reviewed_sha256,
        "review_import_sha256": review_import.get("human_reviewed_text_sha256"),
    }
    return revised


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", required=True, type=Path)
    parser.add_argument("--reviewed-text", required=True, type=Path)
    parser.add_argument("--review-import", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    document = json.loads(args.document.read_text(encoding="utf-8"))
    review_import = json.loads(args.review_import.read_text(encoding="utf-8"))
    revised = apply_review(
        document,
        args.reviewed_text.read_text(encoding="utf-8"),
        review_import,
        sha256_file(args.reviewed_text),
    )
    args.output.write_text(json.dumps(revised, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
