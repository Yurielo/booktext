#!/usr/bin/env python3

import csv
import json
import math
import re
import sys
from difflib import SequenceMatcher


def norm(s):
    return (
        (s or "")
        .lower()
        .replace("ё", "е")
        .replace("–", "-")
        .replace("—", "-")
        .strip()
    )


def sim(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def compact(s):
    return re.sub(r"\s+", "", norm(s))


def vbox(word, W, H):
    b = word["bbox"]
    x1 = float(b["x"]) * W
    x2 = (float(b["x"]) + float(b["width"])) * W
    y1 = (1 - (float(b["y"]) + float(b["height"]))) * H
    y2 = (1 - float(b["y"])) * H
    return (x1, y1, x2, y2)


def center(box):
    return (
        (box[0] + box[2]) / 2,
        (box[1] + box[3]) / 2,
    )


def union_box(items):
    return (
        min(x["box"][0] for x in items),
        min(x["box"][1] for x in items),
        max(x["box"][2] for x in items),
        max(x["box"][3] for x in items),
    )


def load_tess(path):
    out = []

    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")

        for row in reader:
            text = (row.get("text") or "").strip()

            if not text:
                continue

            try:
                conf = float(row.get("conf", "-1"))
            except ValueError:
                continue

            if conf < 0:
                continue

            x = float(row["left"])
            y = float(row["top"])
            w = float(row["width"])
            h = float(row["height"])

            out.append({
                "text": text,
                "conf": conf / 100,
                "box": (x, y, x+w, y+h),
            })

    return out


def group_text(items):
    return " ".join(x["text"] for x in items)


def group_compact(items):
    return "".join(compact(x["text"]) for x in items)


def geometry_cost(A, B):
    ba = union_box(A)
    bb = union_box(B)

    acx, acy = center(ba)
    bcx, bcy = center(bb)

    ah = max(1, ba[3] - ba[1])
    bh = max(1, bb[3] - bb[1])
    avg_h = (ah + bh) / 2

    dy = abs(acy - bcy) / avg_h

    aw = max(1, ba[2] - ba[0])
    bw = max(1, bb[2] - bb[0])

    dx = abs(acx - bcx) / max(20, (aw + bw) / 2)

    return dy * 2.5 + dx * 0.7


def pair_cost(A, B):
    ta = group_text(A)
    tb = group_text(B)

    ca = group_compact(A)
    cb = group_compact(B)

    text_similarity = max(
        sim(ta, tb),
        sim(ca, cb),
    )

    gc = geometry_cost(A, B)

    # Strong reward for visually aligned and textually similar groups.
    return (
        (1 - text_similarity) * 3.0
        + gc
    )


def align(V, T):
    n = len(V)
    m = len(T)

    INF = 10**9

    dp = [[INF] * (m+1) for _ in range(n+1)]
    back = [[None] * (m+1) for _ in range(n+1)]

    dp[0][0] = 0.0

    # Allowed transitions:
    # 1V:1T
    # 1V:2T
    # 2V:1T
    # 1V:3T
    # 3V:1T
    # V-only
    # T-only
    moves = [
        (1, 1, "1:1"),
        (1, 2, "1:2"),
        (2, 1, "2:1"),
        (1, 3, "1:3"),
        (3, 1, "3:1"),
        (1, 0, "V_ONLY"),
        (0, 1, "T_ONLY"),
    ]

    for i in range(n+1):
        for j in range(m+1):

            cur = dp[i][j]

            if cur >= INF:
                continue

            for dv, dt, kind in moves:
                ni = i + dv
                nj = j + dt

                if ni > n or nj > m:
                    continue

                if dv and dt:
                    A = V[i:ni]
                    B = T[j:nj]

                    # Reject groups from clearly different printed lines.
                    ba = union_box(A)
                    bb = union_box(B)

                    _, ay = center(ba)
                    _, by = center(bb)

                    if abs(ay - by) > 18:
                        continue

                    c = pair_cost(A, B)

                    # Mild penalty for grouping, so 1:1 wins when adequate.
                    if dv + dt > 2:
                        c += 0.18 * (dv + dt - 2)

                elif dv:
                    # Missing in Tesseract
                    c = 2.25

                else:
                    # Missing in Vision
                    c = 2.25

                score = cur + c

                if score < dp[ni][nj]:
                    dp[ni][nj] = score
                    back[ni][nj] = (i, j, kind)

    i, j = n, m
    path = []

    while i or j:
        b = back[i][j]

        if b is None:
            raise RuntimeError(f"Alignment failed at {i},{j}")

        pi, pj, kind = b

        path.append({
            "kind": kind,
            "v_start": pi,
            "v_end": i,
            "t_start": pj,
            "t_end": j,
            "vision": V[pi:i],
            "tess": T[pj:j],
        })

        i, j = pi, pj

    path.reverse()
    return path


def main():
    if len(sys.argv) != 5:
        print(
            "usage: hybrid_sequence_align_v8.py "
            "<vision.json> <tess.tsv> <W> <H>"
        )
        raise SystemExit(2)

    vision_path, tess_path, W, H = sys.argv[1:]
    W = int(W)
    H = int(H)

    with open(vision_path, encoding="utf-8") as f:
        d = json.load(f)

    V = []

    for idx, w in enumerate(d["words"], 1):
        V.append({
            "i": idx,
            "text": w["text"],
            "conf": w.get("confidence"),
            "box": vbox(w, W, H),
        })

    T = load_tess(tess_path)

    for idx, t in enumerate(T, 1):
        t["i"] = idx

    path = align(V, T)

    counts = {}

    print("===== HYBRID SEQUENCE ALIGNER V8 =====")
    print("Vision words:   ", len(V))
    print("Tesseract words:", len(T))
    print("Alignment units:", len(path))
    print()

    for u in path:
        counts[u["kind"]] = counts.get(u["kind"], 0) + 1

    print("===== UNIT TYPES =====")
    for k in sorted(counts):
        print(f"{k:8s}: {counts[k]}")

    print()
    print("===== NON-1:1 UNITS =====")

    special = []

    for num, u in enumerate(path, 1):
        if u["kind"] == "1:1":
            continue

        special.append((num, u))

        vt = group_text(u["vision"]) or "∅"
        tt = group_text(u["tess"]) or "∅"

        print()
        print(f"UNIT {num:03d}  {u['kind']}")

        if u["vision"]:
            print(
                "VISION:",
                " | ".join(
                    f"V{x['i']:03d}:{x['text']}"
                    for x in u["vision"]
                )
            )
        else:
            print("VISION: ∅")

        if u["tess"]:
            print(
                "TESS:  ",
                " | ".join(
                    f"T{x['i']:03d}:{x['text']}"
                    for x in u["tess"]
                )
            )
        else:
            print("TESS:   ∅")

        print(f"TEXT:   {vt!r}  <=>  {tt!r}")
        print(
            "SIM:",
            f"{max(sim(vt,tt),sim(compact(vt),compact(tt))):.3f}"
        )

    print()
    print("===== TARGET AREAS =====")

    needles = [
        "витамин",
        "витамив",
        "макароны",
        "обнаруж",
        "ружила",
        "эргот",
        "лелать",
        "делать",
        "лапшу",
        "лапу",
    ]

    for num, u in enumerate(path, 1):
        vt = group_text(u["vision"])
        tt = group_text(u["tess"])
        merged = norm(vt + " " + tt)

        if not any(x in merged for x in needles):
            continue

        print()
        print(f"UNIT {num:03d}  {u['kind']}")
        print("VISION:", vt or "∅")
        print("TESS:  ", tt or "∅")

    outfile = "/tmp/booktext-hybrid-v8-alignment.json"

    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(
            {
                "engine": "booktext-hybrid-sequence-v8",
                "counts": counts,
                "units": [
                    {
                        "kind": u["kind"],
                        "vision": [
                            {
                                "index": x["i"],
                                "text": x["text"],
                                "confidence": x["conf"],
                            }
                            for x in u["vision"]
                        ],
                        "tesseract": [
                            {
                                "index": x["i"],
                                "text": x["text"],
                                "confidence": x["conf"],
                            }
                            for x in u["tess"]
                        ],
                    }
                    for u in path
                ],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("===== OUTPUT =====")
    print(outfile)


if __name__ == "__main__":
    main()
