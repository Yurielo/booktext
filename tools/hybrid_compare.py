#!/usr/bin/env python3

import csv
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path


def normalize_text(text):
    text = text.strip()
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", " ", text)
    return text


def similarity(a, b):
    return SequenceMatcher(
        None,
        normalize_text(a).lower(),
        normalize_text(b).lower()
    ).ratio()


def word_diff(a, b):
    aw = normalize_text(a).split()
    bw = normalize_text(b).split()

    matcher = SequenceMatcher(
        None,
        [x.lower() for x in aw],
        [x.lower() for x in bw]
    )

    changes = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        left = " ".join(aw[i1:i2]) or "∅"
        right = " ".join(bw[j1:j2]) or "∅"

        changes.append(f"{left}  ↔  {right}")

    return changes


def read_vision(path, image_height):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    result = []

    for i, line in enumerate(data.get("lines", []), 1):
        b = line.get("bbox", {})

        x = float(b.get("x", 0))
        y = float(b.get("y", 0))
        w = float(b.get("width", 0))
        h = float(b.get("height", 0))

        # Vision origin is lower-left.
        center_y_px = (1.0 - (y + h / 2.0)) * image_height

        result.append({
            "number": i,
            "text": line.get("text", ""),
            "confidence": line.get("confidence"),
            "center_y": center_y_px,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
        })

    return result


def read_tesseract(path):
    groups = {}

    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="\t")

        for row in reader:
            text = (row.get("text") or "").strip()

            if not text:
                continue

            try:
                conf = float(row.get("conf", "-1"))
            except Exception:
                conf = -1

            if conf < 0:
                continue

            key = (
                row.get("page_num"),
                row.get("block_num"),
                row.get("par_num"),
                row.get("line_num"),
            )

            try:
                left = int(row.get("left", 0))
                top = int(row.get("top", 0))
                width = int(row.get("width", 0))
                height = int(row.get("height", 0))
            except Exception:
                continue

            g = groups.setdefault(
                key,
                {
                    "words": [],
                    "confidences": [],
                    "left": left,
                    "top": top,
                    "right": left + width,
                    "bottom": top + height,
                },
            )

            g["words"].append(text)
            g["confidences"].append(conf)

            g["left"] = min(g["left"], left)
            g["top"] = min(g["top"], top)
            g["right"] = max(g["right"], left + width)
            g["bottom"] = max(g["bottom"], top + height)

    result = []

    for i, (_, g) in enumerate(groups.items(), 1):
        result.append({
            "number": i,
            "text": " ".join(g["words"]),
            "confidence": sum(g["confidences"]) / len(g["confidences"]),
            "center_y": (g["top"] + g["bottom"]) / 2.0,
            "top": g["top"],
            "bottom": g["bottom"],
        })

    result.sort(key=lambda r: r["center_y"])

    for i, row in enumerate(result, 1):
        row["number"] = i

    return result


def match_lines(vision, tess, image_height):
    candidates = []

    for vi, v in enumerate(vision):
        for ti, t in enumerate(tess):
            dy = abs(v["center_y"] - t["center_y"])
            sim = similarity(v["text"], t["text"])

            # Geometry is dominant, text similarity breaks close ties.
            score = (dy / image_height) * 4.0 + (1.0 - sim)

            candidates.append((score, dy, -sim, vi, ti))

    candidates.sort()

    used_v = set()
    used_t = set()
    matches = []

    max_dy = max(25.0, image_height * 0.025)

    for score, dy, neg_sim, vi, ti in candidates:
        if vi in used_v or ti in used_t:
            continue

        if dy > max_dy:
            continue

        used_v.add(vi)
        used_t.add(ti)

        v = vision[vi]
        t = tess[ti]

        matches.append({
            "vision": v,
            "tesseract": t,
            "dy": dy,
            "similarity": -neg_sim,
        })

    unmatched_v = [
        v for i, v in enumerate(vision)
        if i not in used_v
    ]

    unmatched_t = [
        t for i, t in enumerate(tess)
        if i not in used_t
    ]

    matches.sort(key=lambda m: m["vision"]["center_y"])

    return matches, unmatched_v, unmatched_t


def classify(match):
    v = match["vision"]
    t = match["tesseract"]
    sim = match["similarity"]

    vc = v["confidence"] if v["confidence"] is not None else 0
    tc = t["confidence"] / 100.0

    if sim >= 0.985:
        return "AGREE"

    if sim >= 0.90:
        return "SMALL_DIFF"

    if sim < 0.65:
        return "STRONG_DIFF"

    if vc < 0.40 and tc >= 0.80:
        return "TESS_FAVORED"

    if tc < 0.70 and vc >= 0.55:
        return "VISION_FAVORED"

    return "REVIEW"


def main():
    if len(sys.argv) != 5:
        print(
            "Usage: hybrid_compare.py "
            "<vision.json> <tesseract.tsv> <image-width> <image-height>"
        )
        raise SystemExit(2)

    vision_path = sys.argv[1]
    tess_path = sys.argv[2]
    image_width = int(sys.argv[3])
    image_height = int(sys.argv[4])

    vision = read_vision(vision_path, image_height)
    tess = read_tesseract(tess_path)

    matches, unmatched_v, unmatched_t = match_lines(
        vision,
        tess,
        image_height
    )

    print("===== HYBRID COMPARATOR =====")
    print(f"image: {image_width} x {image_height}")
    print(f"Vision lines:    {len(vision)}")
    print(f"Tesseract lines: {len(tess)}")
    print(f"Matched:         {len(matches)}")
    print(f"Vision only:     {len(unmatched_v)}")
    print(f"Tesseract only:  {len(unmatched_t)}")
    print()

    counts = {}

    for m in matches:
        c = classify(m)
        counts[c] = counts.get(c, 0) + 1

    print("===== CLASSIFICATION =====")
    for key in [
        "AGREE",
        "SMALL_DIFF",
        "VISION_FAVORED",
        "TESS_FAVORED",
        "REVIEW",
        "STRONG_DIFF",
    ]:
        print(f"{key:15s}: {counts.get(key, 0)}")

    print()
    print("===== DIFFERENCES =====")

    n = 0

    for m in matches:
        classification = classify(m)

        if classification == "AGREE":
            continue

        n += 1

        v = m["vision"]
        t = m["tesseract"]

        vc = v["confidence"]
        vc_text = "None" if vc is None else f"{vc:.4f}"

        print()
        print(
            f"--- #{n} {classification} "
            f"| similarity={m['similarity']:.3f} "
            f"| Δy={m['dy']:.1f}px ---"
        )
        print(
            f"VISION     conf={vc_text:>6} "
            f"line={v['number']:02d}: {v['text']}"
        )
        print(
            f"TESSERACT  conf={t['confidence']:6.2f} "
            f"line={t['number']:02d}: {t['text']}"
        )

        changes = word_diff(v["text"], t["text"])

        if changes:
            print("DIFF:")
            for change in changes[:12]:
                print("  ", change)

    if unmatched_v:
        print()
        print("===== VISION ONLY =====")
        for v in unmatched_v:
            vc = v["confidence"]
            vc_text = "None" if vc is None else f"{vc:.4f}"
            print(
                f"V{v['number']:02d} conf={vc_text}: "
                f"{v['text']}"
            )

    if unmatched_t:
        print()
        print("===== TESSERACT ONLY =====")
        for t in unmatched_t:
            print(
                f"T{t['number']:02d} conf={t['confidence']:.2f}: "
                f"{t['text']}"
            )


if __name__ == "__main__":
    main()
