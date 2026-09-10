#!/usr/bin/env python3

import csv
import json
import re
import sys
from difflib import SequenceMatcher


def norm(text):
    text = (text or "").strip()
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", " ", text)
    return text


def sim(a, b):
    return SequenceMatcher(
        None,
        norm(a).lower(),
        norm(b).lower()
    ).ratio()


def word_diff(a, b):
    aw = norm(a).split()
    bw = norm(b).split()

    m = SequenceMatcher(
        None,
        [x.lower() for x in aw],
        [x.lower() for x in bw],
    )

    out = []

    for tag, i1, i2, j1, j2 in m.get_opcodes():
        if tag == "equal":
            continue

        left = " ".join(aw[i1:i2]) or "∅"
        right = " ".join(bw[j1:j2]) or "∅"
        out.append(f"{left}  ↔  {right}")

    return out


def read_vision(path, image_height):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    out = []

    for i, line in enumerate(data.get("lines", []), 1):
        b = line.get("bbox", {})

        y = float(b.get("y", 0))
        h = float(b.get("height", 0))

        # Vision normalized coordinates use bottom-left origin.
        top = (1.0 - (y + h)) * image_height
        bottom = (1.0 - y) * image_height

        out.append({
            "engine": "vision",
            "number": i,
            "text": line.get("text", ""),
            "confidence": line.get("confidence"),
            "top": top,
            "bottom": bottom,
        })

    out.sort(key=lambda x: (x["top"], x["number"]))
    return out


def read_tesseract(path):
    groups = {}

    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="\t")

        for row in reader:
            text = (row.get("text") or "").strip()
            if not text:
                continue

            try:
                confidence = float(row.get("conf", "-1"))
            except Exception:
                confidence = -1

            if confidence < 0:
                continue

            key = (
                row.get("page_num"),
                row.get("block_num"),
                row.get("par_num"),
                row.get("line_num"),
            )

            try:
                top = int(row.get("top", 0))
                height = int(row.get("height", 0))
            except Exception:
                continue

            bottom = top + height

            g = groups.setdefault(
                key,
                {
                    "words": [],
                    "conf": [],
                    "top": top,
                    "bottom": bottom,
                },
            )

            g["words"].append(text)
            g["conf"].append(confidence)
            g["top"] = min(g["top"], top)
            g["bottom"] = max(g["bottom"], bottom)

    out = []

    for g in groups.values():
        out.append({
            "engine": "tesseract",
            "text": " ".join(g["words"]),
            "confidence": sum(g["conf"]) / len(g["conf"]),
            "top": g["top"],
            "bottom": g["bottom"],
        })

    out.sort(key=lambda x: x["top"])

    for i, row in enumerate(out, 1):
        row["number"] = i

    return out


def combine(rows):
    return {
        "text": " ".join(norm(x["text"]) for x in rows),
        "top": min(x["top"] for x in rows),
        "bottom": max(x["bottom"] for x in rows),
    }


def centers_close(rows, image_height):
    if len(rows) < 2:
        return True

    centers = [
        (x["top"] + x["bottom"]) / 2
        for x in rows
    ]

    # Multiple OCR lines may actually belong to one physical row,
    # such as a page number and centered running title.
    return max(centers) - min(centers) <= image_height * 0.025


def alignment_cost(vrows, trows, image_height):
    vb = combine(vrows)
    tb = combine(trows)

    similarity = sim(vb["text"], tb["text"])

    vc = (vb["top"] + vb["bottom"]) / 2
    tc = (tb["top"] + tb["bottom"]) / 2

    dy = abs(vc - tc) / image_height

    # Text is primary; geometry prevents unrelated lines matching.
    cost = (1.0 - similarity) * 2.4 + dy * 8.0

    if len(vrows) != 1 or len(trows) != 1:
        cost += 0.05

    return cost


def align(vision, tess, image_height):
    n = len(vision)
    m = len(tess)

    INF = 10**9

    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    prev = [[None] * (m + 1) for _ in range(n + 1)]

    dp[0][0] = 0.0

    GAP = 1.15

    for i in range(n + 1):
        for j in range(m + 1):
            here = dp[i][j]

            if here >= INF:
                continue

            # 1 Vision : 1 Tesseract
            if i < n and j < m:
                c = alignment_cost(
                    [vision[i]],
                    [tess[j]],
                    image_height,
                )

                if here + c < dp[i + 1][j + 1]:
                    dp[i + 1][j + 1] = here + c
                    prev[i + 1][j + 1] = (i, j, 1, 1)

            # 2 Vision : 1 Tesseract
            if i + 1 < n and j < m:
                vr = vision[i:i + 2]

                if centers_close(vr, image_height):
                    c = alignment_cost(
                        vr,
                        [tess[j]],
                        image_height,
                    )

                    if here + c < dp[i + 2][j + 1]:
                        dp[i + 2][j + 1] = here + c
                        prev[i + 2][j + 1] = (i, j, 2, 1)

            # 1 Vision : 2 Tesseract
            if i < n and j + 1 < m:
                tr = tess[j:j + 2]

                if centers_close(tr, image_height):
                    c = alignment_cost(
                        [vision[i]],
                        tr,
                        image_height,
                    )

                    if here + c < dp[i + 1][j + 2]:
                        dp[i + 1][j + 2] = here + c
                        prev[i + 1][j + 2] = (i, j, 1, 2)

            # unmatched Vision
            if i < n:
                if here + GAP < dp[i + 1][j]:
                    dp[i + 1][j] = here + GAP
                    prev[i + 1][j] = (i, j, 1, 0)

            # unmatched Tesseract
            if j < m:
                if here + GAP < dp[i][j + 1]:
                    dp[i][j + 1] = here + GAP
                    prev[i][j + 1] = (i, j, 0, 1)

    result = []

    i, j = n, m

    while i > 0 or j > 0:
        p = prev[i][j]

        if p is None:
            raise RuntimeError(
                f"Alignment traceback failed at {i},{j}"
            )

        pi, pj, nv, nt = p

        result.append({
            "vision": vision[pi:pi + nv],
            "tesseract": tess[pj:pj + nt],
        })

        i, j = pi, pj

    result.reverse()
    return result


def average_conf(rows, engine):
    vals = [
        x["confidence"]
        for x in rows
        if x.get("confidence") is not None
    ]

    if not vals:
        return None

    value = sum(vals) / len(vals)

    if engine == "tesseract":
        value /= 100.0

    return value


def classify(vrows, trows):
    if not vrows:
        return "TESS_ONLY"

    if not trows:
        return "VISION_ONLY"

    vt = combine(vrows)["text"]
    tt = combine(trows)["text"]

    s = sim(vt, tt)

    vc = average_conf(vrows, "vision") or 0
    tc = average_conf(trows, "tesseract") or 0

    if s >= 0.985:
        return "AGREE"

    if s >= 0.94:
        return "SMALL_DIFF"

    if vc < 0.40 and tc >= 0.80:
        return "TESS_FAVORED"

    if tc < 0.70 and vc >= 0.55:
        return "VISION_FAVORED"

    if s < 0.72:
        return "STRONG_DIFF"

    return "REVIEW"


def labels(rows, prefix):
    if not rows:
        return "-"

    return "+".join(
        f"{prefix}{x['number']:02d}"
        for x in rows
    )


def main():
    if len(sys.argv) != 5:
        print(
            "Usage: hybrid_compare_v2.py "
            "<vision.json> <tesseract.tsv> <width> <height>"
        )
        raise SystemExit(2)

    vision_path = sys.argv[1]
    tess_path = sys.argv[2]
    width = int(sys.argv[3])
    height = int(sys.argv[4])

    vision = read_vision(vision_path, height)
    tess = read_tesseract(tess_path)

    aligned = align(vision, tess, height)

    print("===== HYBRID COMPARATOR V2 =====")
    print(f"image: {width} x {height}")
    print(f"Vision lines:    {len(vision)}")
    print(f"Tesseract lines: {len(tess)}")
    print(f"Alignment units: {len(aligned)}")

    counts = {}

    for unit in aligned:
        c = classify(
            unit["vision"],
            unit["tesseract"],
        )
        counts[c] = counts.get(c, 0) + 1

    print()
    print("===== CLASSIFICATION =====")

    for key in [
        "AGREE",
        "SMALL_DIFF",
        "VISION_FAVORED",
        "TESS_FAVORED",
        "REVIEW",
        "STRONG_DIFF",
        "VISION_ONLY",
        "TESS_ONLY",
    ]:
        print(f"{key:15s}: {counts.get(key, 0)}")

    print()
    print("===== MERGED ALIGNMENTS =====")

    merges = 0

    for unit in aligned:
        vr = unit["vision"]
        tr = unit["tesseract"]

        if len(vr) == 1 and len(tr) == 1:
            continue

        if not vr or not tr:
            continue

        merges += 1

        print()
        print(
            f"{labels(vr,'V')}  <=>  "
            f"{labels(tr,'T')}"
        )
        print("VISION:    ", combine(vr)["text"])
        print("TESSERACT: ", combine(tr)["text"])
        print(
            "similarity: ",
            f"{sim(combine(vr)['text'], combine(tr)['text']):.4f}"
        )

    if merges == 0:
        print("No merged alignments.")

    print()
    print("===== DIFFERENCES =====")

    number = 0

    for unit in aligned:
        vr = unit["vision"]
        tr = unit["tesseract"]

        c = classify(vr, tr)

        if c == "AGREE":
            continue

        number += 1

        vt = combine(vr)["text"] if vr else ""
        tt = combine(tr)["text"] if tr else ""

        vc = average_conf(vr, "vision")
        tc = average_conf(tr, "tesseract")

        vc_text = "None" if vc is None else f"{vc:.4f}"
        tc_text = "None" if tc is None else f"{tc:.4f}"

        similarity = sim(vt, tt) if vr and tr else 0

        print()
        print(
            f"--- #{number} {c} "
            f"| {labels(vr,'V')} <=> {labels(tr,'T')} "
            f"| similarity={similarity:.3f} ---"
        )
        print(
            f"VISION     conf={vc_text}: {vt or '∅'}"
        )
        print(
            f"TESSERACT  conf={tc_text}: {tt or '∅'}"
        )

        if vr and tr:
            changes = word_diff(vt, tt)

            if changes:
                print("DIFF:")

                for change in changes[:15]:
                    print("  ", change)


if __name__ == "__main__":
    main()
