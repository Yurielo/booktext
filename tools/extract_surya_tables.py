#!/usr/bin/env python3
"""Extract Surya HTML tables without guessing cell values or corrections."""

import argparse
import csv
import hashlib
import html
import json
import re
from pathlib import Path


def clean_html_text(s):
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</?(?:i|b|strong|em|span)[^>]*>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def parse_table(table_html):
    rows = []

    for row_html in re.findall(
        r"<tr\b[^>]*>(.*?)</tr>",
        table_html,
        flags=re.I | re.S
    ):
        cells = []

        for m in re.finditer(
            r"<(td|th)\b([^>]*)>(.*?)</\1>",
            row_html,
            flags=re.I | re.S
        ):
            tag = m.group(1).lower()
            attrs = m.group(2)
            body = m.group(3)

            def attr_int(name, default=1):
                mm = re.search(
                    rf'{name}\s*=\s*["\']?(\d+)',
                    attrs,
                    flags=re.I
                )
                return int(mm.group(1)) if mm else default

            cells.append({
                "type": tag,
                "text": clean_html_text(body),
                "rowspan": attr_int("rowspan"),
                "colspan": attr_int("colspan"),
                "html": body.strip(),
            })

        if cells:
            rows.append(cells)

    return rows


def expand_grid(rows):
    """
    Expand rowspan/colspan into a rectangular grid.
    Origin cells keep their text; covered cells are blank.
    """
    grid = []
    occupied = {}

    for r_idx, row in enumerate(rows):
        if len(grid) <= r_idx:
            grid.append([])

        c_idx = 0

        for cell in row:
            while occupied.get((r_idx, c_idx)):
                c_idx += 1

            rs = max(1, cell["rowspan"])
            cs = max(1, cell["colspan"])

            while len(grid[r_idx]) < c_idx:
                grid[r_idx].append("")

            for rr in range(r_idx, r_idx + rs):
                while len(grid) <= rr:
                    grid.append([])

                while len(grid[rr]) < c_idx + cs:
                    grid[rr].append("")

            grid[r_idx][c_idx] = cell["text"]

            for rr in range(r_idx, r_idx + rs):
                for cc in range(c_idx, c_idx + cs):
                    if rr == r_idx and cc == c_idx:
                        continue
                    occupied[(rr, cc)] = True

            c_idx += cs

    width = max((len(r) for r in grid), default=0)

    for r in grid:
        r.extend([""] * (width - len(r)))

    return grid


def get_page(data, page_number):
    key = f"page-{page_number:04d}"

    if key not in data:
        return None

    obj = data[key]
    return obj[0] if isinstance(obj, list) and obj else obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--pages", required=True)
    args = ap.parse_args()

    src = Path(args.results)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    source_bytes = src.read_bytes()
    data = json.loads(source_bytes.decode("utf-8"))
    source = {
        "filename": src.name,
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
    }

    pages = [
        int(x)
        for x in re.split(r"[,\s]+", args.pages.strip())
        if x
    ]

    manifest = []

    print("===== SURYA TABLE EXTRACTION =====")

    for page_number in pages:
        page = get_page(data, page_number)

        if not isinstance(page, dict):
            print(f"PAGE {page_number:03d}: MISSING")
            manifest.append({
                "page": page_number,
                "status": "MISSING",
                "tables": [],
            })
            continue

        blocks = page.get("blocks") or []

        tables = [
            b for b in blocks
            if isinstance(b, dict)
            and str(b.get("label", "")).lower() == "table"
        ]

        if not tables:
            review_regions = [
                block for block in blocks
                if isinstance(block, dict)
                and str(block.get("label", "")).lower() == "figure"
                and block.get("table_context") is True
            ]
            if review_regions:
                print(f"PAGE {page_number:03d}: FALLBACK_REVIEW")
                manifest.append({
                    "page": page_number,
                    "status": "FALLBACK_REVIEW",
                    "tables": [
                        {
                            "index": index,
                            "status": "fallback_review",
                            "source": "surya_figure_table_context",
                            "review_required": True,
                            "diagnostic": "skipped_figure_explicitly_marked_table_context",
                            "bbox": block.get("bbox"),
                            "confidence": block.get("confidence"),
                        }
                        for index, block in enumerate(review_regions, start=1)
                    ],
                })
            else:
                print(f"PAGE {page_number:03d}: NO_TABLE_BLOCK")
                manifest.append({"page": page_number, "status": "NO_TABLE_BLOCK", "tables": []})
            continue

        page_entry = {
            "page": page_number,
            "status": "OK",
            "tables": []
        }

        for table_index, block in enumerate(tables, start=1):
            raw_html = block.get("html", "") or ""

            if "<table" not in raw_html.lower():
                status = (
                    "fallback_review"
                    if "<img" in raw_html.lower()
                    else "fallback_review"
                )

                print(
                    f"PAGE {page_number:03d} "
                    f"TABLE {table_index:02d}: {status}"
                )

                page_entry["tables"].append({
                    "index": table_index,
                    "status": status,
                    "source": "surya_table_block",
                    "review_required": True,
                    "diagnostic": "image_only_or_non_html_table_block",
                    "bbox": block.get("bbox"),
                    "confidence": block.get("confidence"),
                })
                continue

            parsed_rows = parse_table(raw_html)
            grid = expand_grid(parsed_rows)

            stem = (
                f"page-{page_number:04d}"
                f"-table-{table_index:02d}"
            )

            json_path = out / f"{stem}.json"
            tsv_path = out / f"{stem}.tsv"
            html_path = out / f"{stem}.html"

            json_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "artifact_type": "booktext.surya_html_table",
                        "source": source,
                        "page": page_number,
                        "table_index": table_index,
                        "bbox": block.get("bbox"),
                        "confidence": block.get("confidence"),
                        "rows": parsed_rows,
                        "grid": grid,
                        "row_count": len(grid),
                        "column_count": max(
                            (len(x) for x in grid),
                            default=0
                        ),
                    },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
                encoding="utf-8"
            )

            with tsv_path.open(
                "w",
                encoding="utf-8",
                newline=""
            ) as f:
                w = csv.writer(f, delimiter="\t")
                for row in grid:
                    w.writerow(row)

            html_path.write_text(
                raw_html,
                encoding="utf-8"
            )

            columns = max(
                (len(x) for x in grid),
                default=0
            )

            print(
                f"PAGE {page_number:03d} "
                f"TABLE {table_index:02d}: "
                f"rows={len(grid)} cols={columns}"
            )

            page_entry["tables"].append({
                "index": table_index,
                "status": "HTML_TABLE",
                "rows": len(grid),
                "columns": columns,
                "json": json_path.name,
                "tsv": tsv_path.name,
                "html": html_path.name,
                "bbox": block.get("bbox"),
                "confidence": block.get("confidence"),
            })

        # A Figure is never treated as a table based on geometry alone.  A
        # producer must explicitly flag table_context before it becomes a
        # review region; no cells are reconstructed here.
        for block in blocks:
            if (
                isinstance(block, dict)
                and str(block.get("label", "")).lower() == "figure"
                and block.get("table_context") is True
            ):
                page_entry["tables"].append({
                    "index": len(page_entry["tables"]) + 1,
                    "status": "fallback_review",
                    "source": "surya_figure_table_context",
                    "review_required": True,
                    "diagnostic": "skipped_figure_explicitly_marked_table_context",
                    "bbox": block.get("bbox"),
                    "confidence": block.get("confidence"),
                })

        manifest.append(page_entry)

    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "artifact_type": "booktext.surya_table_manifest",
                "source": source,
                "pages": manifest
            },
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    print()
    print("MANIFEST:", manifest_path)


if __name__ == "__main__":
    main()
