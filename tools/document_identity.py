"""Stable, user-facing identity for BookText output folders.

Canonical artifacts keep their fixed paths for Core and Integra.  This module
adds title-named *copies* for a person who moves a generated file outside its
BookText folder, where names such as ``book_final.md`` would be ambiguous.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path


SCHEMA_VERSION = "1.0"
FILENAME = "document_identity.json"


def safe_title(value):
    """Return a portable filename stem while retaining ordinary Unicode text."""
    value = re.sub(r"[\\/:\x00]", "-", str(value or "").strip())
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:180] or "BookText document")


def title_from_sources(sources):
    values = [Path(value) for value in sources if str(value)]
    if len(values) == 1:
        return values[0].stem or values[0].name
    return "BookText image collection" if values else "BookText document"


def identity_for_sources(sources, output_dir):
    title = title_from_sources(sources)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "booktext.document_identity",
        "title": title,
        "safe_title": safe_title(title),
        "source_files": [Path(value).name for value in sources],
        "output_directory": Path(output_dir).name,
        "canonical_artifacts": {},
        "named_deliverables": {},
        "policy": "Canonical BookText artifact paths stay stable. Named deliverables are portable user-facing copies tied to this source document.",
    }


def _load_or_create(output_dir, sources=()):
    root = Path(output_dir)
    path = root / FILENAME
    if path.is_file():
        try:
            identity = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid document identity artifact: {error}") from error
        if identity.get("artifact_type") != "booktext.document_identity" or identity.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported document identity artifact")
        return identity
    return identity_for_sources(sources, root)


def _write(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def initialize(output_dir, sources):
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    identity = _load_or_create(root, sources)
    _write(root / FILENAME, identity)
    return identity


def register_deliverable(output_dir, canonical_relative_path, label, *, sources=()):
    """Copy one canonical artifact to a title-named portable deliverable."""
    root = Path(output_dir)
    canonical = root / canonical_relative_path
    if not canonical.is_file():
        raise ValueError(f"canonical deliverable is missing: {canonical_relative_path}")
    identity = _load_or_create(root, sources)
    deliverables = root / "deliverables"
    deliverables.mkdir(exist_ok=True)
    suffix = canonical.suffix
    filename = f"{identity['safe_title']} — {safe_title(label)}{suffix}"
    destination = deliverables / filename
    shutil.copyfile(canonical, destination)
    identity.setdefault("canonical_artifacts", {})[str(canonical_relative_path)] = str(canonical_relative_path)
    identity.setdefault("named_deliverables", {})[str(canonical_relative_path)] = str(destination.relative_to(root))
    _write(root / FILENAME, identity)
    return destination.relative_to(root)

