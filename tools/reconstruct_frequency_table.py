#!/usr/bin/env python3
"""Reconstruct frequency-table rows from existing Surya and Vision output.

The extractor is deliberately conservative: Surya remains the primary OCR,
Vision supplies independent geometric evidence, and disagreements are flagged
instead of being silently corrected.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_DATA_ROOT = (
    Path.home()
    / "Library/Mobile Documents/com~apple~CloudDocs/Downloads"
    / "neizlechimyh_bolezney_net_book/pages/page-0001_book"
)
CELL_NAMES = ("name", "low", "high", "extra")
NUMBER_LIKE = re.compile(r"^[\s$€£]?\d[\d\s.,-]*$")


class TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._in_tbody = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tbody" and self._table is not None:
            self._in_tbody = True
        elif tag == "tr" and self._table is not None and self._in_tbody:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell_parts is not None and self._row is not None:
            value = " ".join("".join(self._cell_parts).split())
            self._row.append(value)
            self._cell_parts = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
            self._in_tbody = False
        elif tag == "tbody":
            self._in_tbody = False


@dataclass(frozen=True)
class VisionLine:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float | None

    @property
    def bbox(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def canonical_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value)).casefold()
    text = text.replace("ı", "i").replace("ё", "е")
    return "".join(char for char in text if char.isalnum())


def canonical_number(value: str | None) -> str:
    text = clean_text(value).replace(",", ".")
    text = re.sub(r"(?<=\d)\s+(?=\d)", ".", text)
    text = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"\d+(?:\.\d+)*", text):
        return canonical_text(value)
    parts = text.split(".")
    return ".".join(str(int(part)) for part in parts)


def is_number_like(value: str | None) -> bool:
    return bool(NUMBER_LIKE.fullmatch(clean_text(value)))


def load_surya_rows(results_path: Path, page: int) -> tuple[list[list[str]], float | None]:
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    page_results = payload.get(f"page-{page:04d}")
    if not page_results:
        raise ValueError(f"Surya page page-{page:04d} is missing in {results_path}")

    candidates: list[tuple[list[list[str]], float | None]] = []
    for page_result in page_results:
        for block in page_result.get("blocks", []):
            if block.get("label") != "Table" or not block.get("html"):
                continue
            parser = TableHTMLParser()
            parser.feed(block["html"])
            candidates.extend((table, block.get("confidence")) for table in parser.tables)

    if not candidates:
        raise ValueError(f"Surya did not produce a table for page {page}")

    rows, confidence = max(candidates, key=lambda item: len(item[0]))
    rows = [row for row in rows if row and "pathog" not in canonical_text(row[0])]
    return rows, confidence


def load_vision_lines(vision_path: Path) -> list[VisionLine]:
    payload = json.loads(vision_path.read_text(encoding="utf-8"))
    lines = []
    for item in payload.get("lines", []):
        bbox = item.get("bbox") or {}
        text = clean_text(item.get("text"))
        if not text:
            continue
        lines.append(
            VisionLine(
                text=text,
                x=float(bbox.get("x", 0.0)),
                y=float(bbox.get("y", 0.0)),
                width=float(bbox.get("width", 0.0)),
                height=float(bbox.get("height", 0.0)),
                confidence=item.get("confidence"),
            )
        )
    return lines


def cluster_x_positions(lines: list[VisionLine]) -> list[tuple[float, int]]:
    candidates = [
        line.x
        for line in lines
        if 0.2 < line.x < 0.9 and line.y < 0.93 and is_number_like(line.text)
    ]
    clusters: list[list[float]] = []
    for value in sorted(candidates):
        if not clusters or value - statistics.mean(clusters[-1]) > 0.045:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return [(statistics.median(cluster), len(cluster)) for cluster in clusters]


def detect_columns(lines: list[VisionLine], include_extra: bool) -> list[float]:
    low_header = next((line for line in lines if canonical_text(line.text) == "low"), None)
    high_header = next((line for line in lines if canonical_text(line.text) == "high"), None)
    if low_header and high_header:
        centers = [low_header.x, high_header.x]
    else:
        ranked = sorted(cluster_x_positions(lines), key=lambda item: item[1], reverse=True)
        required = 3 if include_extra else 2
        centers = sorted(center for center, count in ranked[:required] if count >= 3)

    if include_extra and len(centers) < 3:
        ranked = sorted(cluster_x_positions(lines), key=lambda item: item[1], reverse=True)
        centers = sorted(center for center, count in ranked[:3] if count >= 3)

    required = 3 if include_extra else 2
    if len(centers) < required:
        raise ValueError(f"Could not detect {required} numeric Vision columns")
    return centers[:required]


def extract_name_lines(lines: list[VisionLine], first_column_x: float) -> list[VisionLine]:
    boundary = max(0.18, first_column_x - 0.10)
    ignored = {"pathogen", "low", "high", "частотыпатогенов"}
    result = []
    for line in lines:
        normalized = canonical_text(line.text)
        if line.x >= boundary or line.y > 0.93 or line.y < 0.012:
            continue
        if normalized in ignored or is_number_like(line.text):
            continue
        if normalized.startswith("sстанистая") or normalized.startswith("вбактерии"):
            continue
        result.append(line)
    return sorted(result, key=lambda line: -line.y)


def similarity(left: str, right: str) -> float:
    a, b = canonical_text(left), canonical_text(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def align_names(
    surya_rows: list[list[str]], vision_lines: list[VisionLine]
) -> list[list[VisionLine] | None]:
    """Monotonically align Surya rows to one or two Vision name lines."""
    count_s, count_v = len(surya_rows), len(vision_lines)
    negative = -10**9
    scores = [[negative] * (count_v + 1) for _ in range(count_s + 1)]
    back: list[list[tuple[int, int, int] | None]] = [
        [None] * (count_v + 1) for _ in range(count_s + 1)
    ]
    scores[0][0] = 0.0

    for i in range(count_s + 1):
        for j in range(count_v + 1):
            current = scores[i][j]
            if current <= negative:
                continue
            if j < count_v and current - 0.18 > scores[i][j + 1]:
                scores[i][j + 1] = current - 0.18
                back[i][j + 1] = (i, j, 0)
            if i < count_s and current - 0.65 > scores[i + 1][j]:
                scores[i + 1][j] = current - 0.65
                back[i + 1][j] = (i, j, -1)
            if i >= count_s:
                continue
            for take in (1, 2):
                if j + take > count_v:
                    continue
                joined = " ".join(line.text for line in vision_lines[j : j + take])
                ratio = similarity(surya_rows[i][0], joined)
                reward = 2.2 * ratio - 0.40 - 0.08 * (take - 1)
                if current + reward > scores[i + 1][j + take]:
                    scores[i + 1][j + take] = current + reward
                    back[i + 1][j + take] = (i, j, take)

    end_j = max(range(count_v + 1), key=lambda j: scores[count_s][j] - 0.18 * (count_v - j))
    matches: list[list[VisionLine] | None] = [None] * count_s
    i, j = count_s, end_j
    while i or j:
        previous = back[i][j]
        if previous is None:
            break
        old_i, old_j, take = previous
        if take > 0:
            matches[old_i] = vision_lines[old_j:j]
        i, j = old_i, old_j
    return matches


def find_numeric_line(
    lines: list[VisionLine], column_x: float, row_y: float
) -> VisionLine | None:
    candidates = [
        line
        for line in lines
        if abs(line.x - column_x) <= 0.05
        and abs(line.y - row_y) <= 0.011
        and is_number_like(line.text)
    ]
    return min(candidates, key=lambda line: abs(line.y - row_y), default=None)


def make_cell(
    surya: str | None,
    vision_line: VisionLine | None,
    kind: str,
    surya_confidence: float | None,
) -> dict[str, object]:
    surya = clean_text(surya) or None
    vision = clean_text(vision_line.text) if vision_line else None
    canonicalizer = canonical_number if kind != "name" else canonical_text
    agreement = bool(surya and vision and canonicalizer(surya) == canonicalizer(vision))
    review_required = not agreement
    if agreement:
        source = "surya_vision_agreement"
    elif vision:
        source = "surya_primary_disagreement"
    else:
        source = "surya_only"
    return {
        "final": surya,
        "surya": surya,
        "vision": vision,
        "source": source,
        "evidence": {
            "surya_confidence": surya_confidence,
            "vision_confidence": vision_line.confidence if vision_line else None,
            "vision_bbox": vision_line.bbox if vision_line else None,
        },
        "confidence": "confirmed" if agreement else "review",
        "review_required": review_required,
    }


def reconstruct(page: int, data_root: Path) -> dict[str, object]:
    results_path = data_root / "surya/surya-input/results.json"
    vision_path = data_root / f"vision/page-{page:04d}.json"
    surya_rows, surya_confidence = load_surya_rows(results_path, page)

    fourth_values = [row[3] for row in surya_rows if len(row) > 3 and row[3]]
    include_extra = bool(fourth_values) and (
        sum(is_number_like(value) for value in fourth_values) / len(fourth_values) >= 0.4
    )
    vision_lines = load_vision_lines(vision_path)
    column_xs = detect_columns(vision_lines, include_extra)
    name_lines = extract_name_lines(vision_lines, column_xs[0])
    aligned_names = align_names(surya_rows, name_lines)

    output_rows = []
    for index, (surya_row, name_match) in enumerate(zip(surya_rows, aligned_names), start=1):
        name_line = None
        if name_match:
            name_line = VisionLine(
                text=" ".join(line.text for line in name_match),
                x=min(line.x for line in name_match),
                y=max(line.y for line in name_match),
                width=max(line.x + line.width for line in name_match) - min(line.x for line in name_match),
                height=sum(line.height for line in name_match),
                confidence=min(
                    (line.confidence for line in name_match if line.confidence is not None),
                    default=None,
                ),
            )
        row_y = name_line.y if name_line else -1.0
        cells: dict[str, dict[str, object]] = {
            "name": make_cell(surya_row[0] if surya_row else None, name_line, "name", surya_confidence)
        }
        numeric_names = ("low", "high", "extra")[: len(column_xs)]
        for cell_index, (cell_name, column_x) in enumerate(zip(numeric_names, column_xs), start=1):
            vision_line = find_numeric_line(vision_lines, column_x, row_y) if name_line else None
            surya_value = surya_row[cell_index] if len(surya_row) > cell_index else None
            cells[cell_name] = make_cell(surya_value, vision_line, cell_name, surya_confidence)

        output_row: dict[str, object] = {
            "row": index,
            **{name: cells[name]["final"] for name in cells},
            "review_required": any(cell["review_required"] for cell in cells.values()),
            "cells": cells,
        }
        output_rows.append(output_row)

    return {
        "page": page,
        "policy": {
            "primary_ocr": "surya",
            "qa_ocr": "apple_vision",
            "automatic_correction": False,
            "disagreements": "keep Surya primary value and set review_required",
        },
        "sources": {"surya": str(results_path), "vision": str(vision_path)},
        "vision_structure": {
            "tables_count": len(json.loads(vision_path.read_text(encoding="utf-8")).get("tables", [])),
            "line_geometry_used": True,
            "numeric_column_x": column_xs,
        },
        "rows": output_rows,
        "summary": {
            "rows": len(output_rows),
            "confirmed_rows": sum(not row["review_required"] for row in output_rows),
            "review_rows": sum(bool(row["review_required"]) for row in output_rows),
        },
    }


def write_outputs(result: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    page = int(result["page"])
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"page-{page:04d}-table.json"
    tsv_path = output_dir / f"page-{page:04d}-table.tsv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with tsv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["row", "name", "low", "high", "extra", "review_required"],
            delimiter="\t",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(result["rows"])
    return json_path, tsv_path


def print_preview(result: dict[str, object], limit: int) -> None:
    summary = result["summary"]
    print(
        f"Page {result['page']}: {summary['rows']} rows; "
        f"confirmed={summary['confirmed_rows']}; review={summary['review_rows']}"
    )
    print("row\tname\tlow\thigh\textra\treview")
    for row in result["rows"][:limit]:
        values = [
            row.get("row"),
            row.get("name") or "",
            row.get("low") or "",
            row.get("high") or "",
            row.get("extra") or "",
            "yes" if row.get("review_required") else "no",
        ]
        print("\t".join(str(value) for value in values))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", type=int, required=True, help="Original batch page number")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp"))
    parser.add_argument("--preview-rows", type=int, default=12)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.page < 1:
        raise SystemExit("--page must be >= 1")
    result = reconstruct(args.page, args.data_root.expanduser())
    json_path, tsv_path = write_outputs(result, args.output_dir.expanduser())
    print_preview(result, args.preview_rows)
    print(f"JSON: {json_path}")
    print(f"TSV:  {tsv_path}")


if __name__ == "__main__":
    main()
