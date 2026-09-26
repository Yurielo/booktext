#!/usr/bin/env python3
"""Validate a Booktext OCR adapter candidate with Integra's actual validator.

This is a read-only compatibility probe.  It does not import a document or
write to the Integra project.
"""

import argparse
import importlib
import json
import re
import sys
from pathlib import Path


HASH = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_FIELDS = {
    "schema_version", "artifact_type", "source", "provider", "blocks"
}
EXPECTED_SOURCE_FIELDS = {"sha256", "format", "page_count", "revision_id"}
EXPECTED_PROVIDER_FIELDS = {
    "component_id", "implementation_sha256", "model_id", "model_sha256"
}


def load_integra_documents(integra_root: Path):
    module_path = integra_root / "integra_core" / "documents.py"
    if not module_path.is_file():
        raise ValueError(f"Integra document validator not found: {module_path}")

    # Integra's document module uses package-relative imports.  Loading the
    # exact package root keeps those imports bound to the supplied checkout.
    root_text = str(integra_root.resolve())
    sys.path.insert(0, root_text)
    try:
        return importlib.import_module("integra_core.documents")
    finally:
        sys.path.pop(0)


def validate_candidate_shape(candidate):
    if not isinstance(candidate, dict) or set(candidate) != EXPECTED_FIELDS:
        raise ValueError("candidate fields differ from Booktext adapter v0.1")
    if candidate["schema_version"] != "0.1":
        raise ValueError("candidate schema_version must be 0.1")
    if candidate["artifact_type"] != "booktext.integra_ocr_candidate":
        raise ValueError("candidate artifact_type is unsupported")

    source = candidate["source"]
    if not isinstance(source, dict) or set(source) != EXPECTED_SOURCE_FIELDS:
        raise ValueError("candidate source fields differ from Booktext adapter v0.1")
    if not isinstance(source["sha256"], str) or not HASH.fullmatch(source["sha256"]):
        raise ValueError("candidate source SHA-256 is invalid")
    if source["format"] not in {"IMAGE", "PDF", "DOCX", "PPTX"}:
        raise ValueError("candidate source format is not supported by Integra OCR")
    if not isinstance(source["page_count"], int) or source["page_count"] < 1:
        raise ValueError("candidate page_count is invalid")
    if not isinstance(source["revision_id"], str) or not source["revision_id"]:
        raise ValueError("candidate revision_id is required")

    provider = candidate["provider"]
    if not isinstance(provider, dict) or set(provider) != EXPECTED_PROVIDER_FIELDS:
        raise ValueError("candidate provider fields differ from Integra OCR")
    for field in ("component_id", "model_id"):
        if not isinstance(provider[field], str) or not provider[field].strip():
            raise ValueError(f"candidate provider {field} is required")
    for field in ("implementation_sha256", "model_sha256"):
        if not isinstance(provider[field], str) or not HASH.fullmatch(provider[field]):
            raise ValueError(f"candidate provider {field} is invalid")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integra-root", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()

    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    validate_candidate_shape(candidate)
    documents = load_integra_documents(args.integra_root)
    provider, blocks = documents._normalize_ocr(
        {"provider": candidate["provider"], "blocks": candidate["blocks"]},
        candidate["source"]["sha256"],
        candidate["source"]["format"],
        candidate["source"]["page_count"],
    )
    print(
        json.dumps(
            {
                "status": "compatible",
                "provider": provider,
                "blocks": len(blocks),
                "source_revision_id": candidate["source"]["revision_id"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
