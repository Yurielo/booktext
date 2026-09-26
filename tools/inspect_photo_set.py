#!/usr/bin/env python3
"""Record original photo orientation before Booktext preprocessing.

The manifest is evidence only: it records the exact input bytes and the
ImageMagick auto-orientation transform required for the prepared OCR page.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_image(path: Path, auto_orient: bool = False) -> dict[str, Any]:
    command = ["magick", str(path)]
    if auto_orient:
        command.append("-auto-orient")
    command.extend(["-format", "%w|%h|%[orientation]", "info:"])
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    width, height, orientation = result.stdout.strip().split("|", 2)
    return {"width": int(width), "height": int(height), "orientation": orientation}


def inspect_photos(paths: list[Path]) -> dict[str, Any]:
    items = []
    for index, path in enumerate(paths, start=1):
        original = probe_image(path)
        prepared = probe_image(path, auto_orient=True)
        items.append({
            "source_index": index,
            "filename": path.name,
            "sha256": sha256_file(path),
            "original": original,
            "prepared": prepared,
            "transform": "imagemagick:auto-orient",
            "review_required": original["orientation"] != "TopLeft",
        })
    return {"schema_version": "0.1", "artifact_type": "booktext.photo_set", "photos": items}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if any(not path.is_file() for path in args.source):
        parser.error("every --source must be an existing file")
    manifest = inspect_photos(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
