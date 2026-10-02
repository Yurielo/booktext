#!/usr/bin/env python3
try:
    from .diagnostic_artifacts import diagnostic_path
except ImportError:
    from diagnostic_artifacts import diagnostic_path

# Evidence retention is explicitly opted into before tools/models run.
diagnostic_path("")


import csv
import json
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


def compact(s):
    return re.sub(r"\s+", "", norm(s))


def sim(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def vbox(word, W, H):
    b = word["bbox"]

    return (
        float(b["x"]) * W,
        (1 - (float(b["y"]) + float(b["height"]))) * H,
        (float(b["x"]) + float(b["width"])) * W,
        (1 - float(b["y"])) * H,
    )


def load_tess(path):
    out = []

    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):

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


def union(items):
    return (
        min(x["box"][0] for x in items),
        min(x["box"][1] for x in items),
        max(x["box"][2] for x in items),
        max(x["box"][3] for x in items),
    )


def center(b):
    return (
        (b[0] + b[2]) / 2,
        (b[1] + b[3]) / 2,
    )


def text(items):
    return " ".join(x["text"] for x in items)


def horizontal_metrics(A, B):
    a = union(A)
    b = union(B)

    aw = max(1.0, a[2] - a[0])
    bw = max(1.0, b[2] - b[0])

    inter = max(
        0.0,
        min(a[2], b[2]) - max(a[0], b[0])
    )

    overlap_min = inter / max(1.0, min(aw, bw))
    overlap_union = inter / max(
        1.0,
        max(a[2], b[2]) - min(a[0], b[0])
    )

    left_delta = abs(a[0] - b[0])
    right_delta = abs(a[2] - b[2])

    width_ratio = min(aw, bw) / max(aw, bw)

    return {
        "overlap_min": overlap_min,
        "iou_x": overlap_union,
        "left_delta": left_delta,
        "right_delta": right_delta,
        "width_ratio": width_ratio,
    }


def pair_cost(A, B, grouped=False):
    ba = union(A)
    bb = union(B)

    acx, acy = center(ba)
    bcx, bcy = center(bb)

    ah = max(1.0, ba[3] - ba[1])
    bh = max(1.0, bb[3] - bb[1])

    avg_h = (ah + bh) / 2

    dy = abs(acy - bcy) / avg_h

    hm = horizontal_metrics(A, B)

    ta = text(A)
    tb = text(B)

    ts = max(
        sim(ta, tb),
        sim(compact(ta), compact(tb)),
    )

    # Geometry is primary.
    cost = (
        dy * 3.0
        + (1 - hm["iou_x"]) * 2.2
        + (1 - hm["width_ratio"]) * 1.6
        + (1 - ts) * 1.0
    )

    if grouped:
        # A genuine 1:N / N:1 split must occupy almost
        # the same horizontal printed region.
        if hm["overlap_min"] < 0.80:
            return None

        if hm["width_ratio"] < 0.68:
            return None

        if hm["left_delta"] > 20:
            return None

        if hm["right_delta"] > 20:
            return None

        cost += 0.20

    return cost


def align(V, T):
    n = len(V)
    m = len(T)
    INF = 10**9

    dp = [[INF] * (m+1) for _ in range(n+1)]
    back = [[None] * (m+1) for _ in range(n+1)]

    dp[0][0] = 0.0

    moves = [
        (1, 1, "1:1"),
        (1, 2, "1:2"),
        (2, 1, "2:1"),
        (1, 3, "1:3"),
        (3, 1, "3:1"),
        (1, 0, "V_ONLY"),
        (0, 1, "T_ONLY"),
    ]

    GAP_COST = 1.55

    for i in range(n+1):
        for j in range(m+1):

            if dp[i][j] >= INF:
                continue

            for dv, dt, kind in moves:

                ni = i + dv
                nj = j + dt

                if ni > n or nj > m:
                    continue

                if dv and dt:

                    A = V[i:ni]
                    B = T[j:nj]

                    ba = union(A)
                    bb = union(B)

                    _, ay = center(ba)
                    _, by = center(bb)

                    if abs(ay - by) > 14:
                        continue

                    grouped = (dv != 1 or dt != 1)

                    if grouped:
                        group_sim = max(
                            sim(text(A), text(B)),
                            sim(compact(text(A)), compact(text(B))),
                        )

                        best_single = 0.0

                        for aa in A:
                            for bb in B:
                                best_single = max(
                                    best_single,
                                    sim(aa["text"], bb["text"]),
                                    sim(compact(aa["text"]), compact(bb["text"])),
                                )

                        gain = group_sim - best_single

                        # A multi-word alignment is allowed only when
                        # combining tokens materially explains an OCR split.
                        #
                        # Examples:
                        #   обнаружила <=> о + ружила   -> allow
                        #   витамив <=> витамин + С + в -> reject
                        #
                        # This prevents a wide Vision bbox from swallowing
                        # genuine words that Vision completely omitted.
                        if group_sim < 0.80 or gain < 0.05:
                            continue

                    c = pair_cost(
                        A,
                        B,
                        grouped=grouped,
                    )

                    if c is None:
                        continue

                else:
                    c = GAP_COST

                new = dp[i][j] + c

                if new < dp[ni][nj]:
                    dp[ni][nj] = new
                    back[ni][nj] = (i, j, kind)

    i, j = n, m
    path = []

    while i or j:

        step = back[i][j]

        if step is None:
            raise RuntimeError(
                f"alignment failed at {i},{j}"
            )

        pi, pj, kind = step

        path.append({
            "kind": kind,
            "vision": V[pi:i],
            "tess": T[pj:j],
        })

        i, j = pi, pj

    path.reverse()
    return path


def main():
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: align.py vision.json tess.tsv W H"
        )

    vp, tp, W, H = sys.argv[1:]
    W = int(W)
    H = int(H)

    with open(vp, encoding="utf-8") as f:
        d = json.load(f)

    V = []

    for i, w in enumerate(d["words"], 1):
        V.append({
            "i": i,
            "text": w["text"],
            "conf": w.get("confidence"),
            "box": vbox(w, W, H),
        })

    T = load_tess(tp)

    for i, t in enumerate(T, 1):
        t["i"] = i

    path = align(V, T)

    counts = {}

    for u in path:
        counts[u["kind"]] = counts.get(u["kind"], 0) + 1

    print("===== HYBRID SEQUENCE ALIGNER V10 =====")
    print("Vision words:   ", len(V))
    print("Tesseract words:", len(T))
    print("Alignment units:", len(path))

    print()
    print("===== UNIT TYPES =====")

    for k in sorted(counts):
        print(f"{k:8s}: {counts[k]}")

    print()
    print("===== NON-1:1 UNITS =====")

    for num, u in enumerate(path, 1):

        if u["kind"] == "1:1":
            continue

        print()
        print(f"UNIT {num:03d} {u['kind']}")

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

    print()
    print("===== TARGET SEQUENCE =====")

    targets = {
        "лелать", "делать",
        "лапшу", "лапу",
        "витамив", "витамин",
        "с", "макароны",
        "после", "из", "их",
        "приготовления.",
        "обнаружила", "ружила",
    }

    for num, u in enumerate(path, 1):

        vt = text(u["vision"])
        tt = text(u["tess"])

        tokens = set(
            re.findall(
                r"[а-яёa-zс]+|приготовления\.",
                norm(vt + " " + tt),
            )
        )

        if not tokens.intersection(targets):
            continue

        print()
        print(f"UNIT {num:03d} {u['kind']}")
        print("VISION:", vt or "∅")
        print("TESS:  ", tt or "∅")

    out = str(diagnostic_path("booktext-hybrid-v10-alignment.json"))

    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "engine": "hybrid-sequence-v10",
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
    print(out)


if __name__ == "__main__":
    main()
