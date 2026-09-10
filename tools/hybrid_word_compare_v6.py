#!/usr/bin/env python3

import csv
import json
import re
import sys
from difflib import SequenceMatcher


def norm(s):
    return (
        s.lower()
        .replace("ё", "е")
        .replace("–", "-")
        .replace("—", "-")
        .strip()
    )


def core(s):
    return re.sub(
        r"^[^\wА-Яа-яЁё]+|[^\wА-Яа-яЁё-]+$",
        "",
        norm(s),
    )


def sim(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def vision_box_px(word, W, H):
    b = word["bbox"]

    x1 = float(b["x"]) * W
    x2 = (float(b["x"]) + float(b["width"])) * W

    y1 = (
        1.0 - (float(b["y"]) + float(b["height"]))
    ) * H
    y2 = (1.0 - float(b["y"])) * H

    return x1, y1, x2, y2


def center(box):
    x1, y1, x2, y2 = box
    return (
        (x1 + x2) / 2.0,
        (y1 + y2) / 2.0,
    )


def overlap_1d(a1, a2, b1, b2):
    inter = max(
        0.0,
        min(a2, b2) - max(a1, b1),
    )

    den = max(
        1.0,
        min(a2 - a1, b2 - b1),
    )

    return inter / den


def load_tesseract(path):
    words = []

    with open(
        path,
        encoding="utf-8",
        errors="replace",
        newline="",
    ) as f:

        reader = csv.DictReader(
            f,
            delimiter="\t",
        )

        for row in reader:

            text = (
                row.get("text") or ""
            ).strip()

            if not text:
                continue

            try:
                conf = float(
                    row.get("conf", "-1")
                )
            except ValueError:
                conf = -1

            if conf < 0:
                continue

            left = float(row["left"])
            top = float(row["top"])
            width = float(row["width"])
            height = float(row["height"])

            words.append(
                {
                    "text": text,
                    "confidence": conf / 100.0,
                    "bbox_px": (
                        left,
                        top,
                        left + width,
                        top + height,
                    ),
                }
            )

    return words


def candidate_score(v, t):

    vb = v["bbox_px"]
    tb = t["bbox_px"]

    vcx, vcy = center(vb)
    tcx, tcy = center(tb)

    vh = max(
        1.0,
        vb[3] - vb[1],
    )

    th = max(
        1.0,
        tb[3] - tb[1],
    )

    avg_h = max(
        1.0,
        (vh + th) / 2.0,
    )

    dy = abs(vcy - tcy) / avg_h

    dx = abs(vcx - tcx) / max(
        20.0,
        (vb[2] - vb[0])
        + (tb[2] - tb[0]),
    )

    y_overlap = overlap_1d(
        vb[1],
        vb[3],
        tb[1],
        tb[3],
    )

    x_overlap = overlap_1d(
        vb[0],
        vb[2],
        tb[0],
        tb[2],
    )

    text_sim = sim(
        v["text"],
        t["text"],
    )

    score = (
        y_overlap * 4.0
        + x_overlap * 2.0
        + text_sim * 1.2
        - dy * 1.5
        - dx * 0.5
    )

    return score, {
        "dy": dy,
        "dx": dx,
        "y_overlap": y_overlap,
        "x_overlap": x_overlap,
        "similarity": text_sim,
    }


def greedy_match(V, T):

    candidates = []

    for vi, v in enumerate(V):

        vb = v["bbox_px"]
        _, vcy = center(vb)

        for ti, t in enumerate(T):

            tb = t["bbox_px"]
            _, tcy = center(tb)

            vh = max(
                1.0,
                vb[3] - vb[1],
            )

            th = max(
                1.0,
                tb[3] - tb[1],
            )

            avg_h = (
                vh + th
            ) / 2.0

            if abs(vcy - tcy) > max(
                12.0,
                avg_h * 1.25,
            ):
                continue

            score, meta = candidate_score(
                v,
                t,
            )

            if (
                meta["x_overlap"] <= 0
                and meta["dx"] > 0.75
            ):
                continue

            candidates.append(
                (
                    score,
                    vi,
                    ti,
                    meta,
                )
            )

    candidates.sort(
        reverse=True,
        key=lambda x: x[0],
    )

    used_v = set()
    used_t = set()
    matches = []

    for score, vi, ti, meta in candidates:

        if (
            vi in used_v
            or ti in used_t
        ):
            continue

        if score < 1.0:
            continue

        used_v.add(vi)
        used_t.add(ti)

        matches.append(
            {
                "vi": vi,
                "ti": ti,
                "score": score,
                **meta,
            }
        )

    return matches, used_v, used_t


def main():

    if len(sys.argv) != 5:
        print(
            "Usage: hybrid_word_compare_v6.py "
            "<vision.json> <tesseract.tsv> "
            "<width> <height>"
        )
        raise SystemExit(2)

    vision_path = sys.argv[1]
    tess_path = sys.argv[2]
    W = int(sys.argv[3])
    H = int(sys.argv[4])

    with open(
        vision_path,
        encoding="utf-8",
    ) as f:
        vision = json.load(f)

    V = []

    for w in vision.get(
        "words",
        [],
    ):
        V.append(
            {
                "text": w["text"],
                "confidence": w.get(
                    "confidence"
                ),
                "bbox_px": vision_box_px(
                    w,
                    W,
                    H,
                ),
            }
        )

    T = load_tesseract(
        tess_path
    )

    matches, used_v, used_t = (
        greedy_match(
            V,
            T,
        )
    )

    matches.sort(
        key=lambda m: (
            center(
                V[m["vi"]]["bbox_px"]
            )[1],
            center(
                V[m["vi"]]["bbox_px"]
            )[0],
        )
    )

    print(
        "===== HYBRID WORD COMPARATOR V6 ====="
    )

    print(
        "Vision words:   ",
        len(V),
    )

    print(
        "Tesseract words:",
        len(T),
    )

    print(
        "Matched:        ",
        len(matches),
    )

    print(
        "Vision only:    ",
        len(V) - len(used_v),
    )

    print(
        "Tesseract only: ",
        len(T) - len(used_t),
    )

    diffs = []

    for m in matches:

        v = V[m["vi"]]
        t = T[m["ti"]]

        if norm(
            v["text"]
        ) == norm(
            t["text"]
        ):
            continue

        diffs.append(
            (
                m,
                v,
                t,
            )
        )

    print()
    print(
        "Different matched words:",
        len(diffs),
    )

    print()
    print(
        "===== ALL WORD DIFFERENCES ====="
    )

    for n, (m, v, t) in enumerate(
        diffs,
        1,
    ):

        vc = (
            v["confidence"]
            if v["confidence"] is not None
            else -1
        )

        print(
            f"{n:03d} "
            f"sim={m['similarity']:.3f} "
            f"yov={m['y_overlap']:.2f} "
            f"xov={m['x_overlap']:.2f}"
        )

        print(
            f"    V {vc:.3f}: "
            f"{v['text']}"
        )

        print(
            f"    T {t['confidence']:.3f}: "
            f"{t['text']}"
        )

    print()
    print(
        "===== SUSPICIOUS / IMPORTANT ====="
    )

    needles = [
        "клет",
        "каж",
        "стра",
        "лает",
        "дает",
        "кукуруз",
        "лел",
        "дел",
        "лап",
        "подог",
        "добав",
        "дерев",
        "лерев",
        "пласт",
        "спорын",
        "пове",
        "зам",
        "поп",
        "заражен",
    ]

    for m, v, t in diffs:

        joined = (
            core(v["text"])
            + " "
            + core(t["text"])
        )

        if not any(
            n in joined
            for n in needles
        ):
            continue

        vc = (
            v["confidence"]
            if v["confidence"] is not None
            else -1
        )

        print()

        print(
            f"V{m['vi']+1:03d} "
            f"conf={vc:.4f} "
            f"| {v['text']}"
        )

        print(
            f"T{m['ti']+1:03d} "
            f"conf={t['confidence']:.4f} "
            f"| {t['text']}"
        )

        print(
            "geometry: "
            f"yOverlap={m['y_overlap']:.3f} "
            f"xOverlap={m['x_overlap']:.3f} "
            f"sim={m['similarity']:.3f}"
        )

    print()
    print(
        "===== UNMATCHED VISION WORDS ====="
    )

    for i, v in enumerate(V):

        if i in used_v:
            continue

        vc = (
            v["confidence"]
            if v["confidence"] is not None
            else -1
        )

        print(
            f"V{i+1:03d} "
            f"conf={vc:.3f} "
            f"| {v['text']}"
        )

    print()
    print(
        "===== UNMATCHED TESSERACT WORDS ====="
    )

    for i, t in enumerate(T):

        if i in used_t:
            continue

        print(
            f"T{i+1:03d} "
            f"conf={t['confidence']:.3f} "
            f"| {t['text']}"
        )


if __name__ == "__main__":
    main()
