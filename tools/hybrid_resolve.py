#!/usr/bin/env python3

import csv
import json
import re
import sys
from difflib import SequenceMatcher


def norm(s):
    s = (s or "").strip()
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"\s+", " ", s)
    return s


def similarity(a, b):
    return SequenceMatcher(
        None,
        norm(a).lower(),
        norm(b).lower()
    ).ratio()


def vision_lines(path, image_height):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    out = []

    for i, line in enumerate(data.get("lines", []), 1):
        b = line.get("bbox", {})
        y = float(b.get("y", 0))
        h = float(b.get("height", 0))

        top = (1.0 - (y + h)) * image_height
        bottom = (1.0 - y) * image_height

        out.append({
            "number": i,
            "text": norm(line.get("text", "")),
            "confidence": line.get("confidence"),
            "top": top,
            "bottom": bottom,
        })

    out.sort(key=lambda r: r["top"])
    return out


def tesseract_lines(path):
    groups = {}

    with open(path, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            text = norm(row.get("text", ""))

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

            top = int(row.get("top", 0))
            height = int(row.get("height", 0))
            bottom = top + height

            g = groups.setdefault(
                key,
                {
                    "words": [],
                    "word_conf": [],
                    "top": top,
                    "bottom": bottom,
                },
            )

            g["words"].append(text)
            g["word_conf"].append(conf)
            g["top"] = min(g["top"], top)
            g["bottom"] = max(g["bottom"], bottom)

    out = []

    for g in groups.values():
        out.append({
            "text": " ".join(g["words"]),
            "confidence": sum(g["word_conf"]) / len(g["word_conf"]),
            "word_conf": g["word_conf"],
            "words": g["words"],
            "top": g["top"],
            "bottom": g["bottom"],
        })

    out.sort(key=lambda r: r["top"])

    for i, row in enumerate(out, 1):
        row["number"] = i

    return out


def combine(rows):
    return " ".join(r["text"] for r in rows).strip()


def center(r):
    return (r["top"] + r["bottom"]) / 2


def close_pair(rows, height):
    if len(rows) < 2:
        return True

    centers = [center(r) for r in rows]
    return max(centers) - min(centers) <= height * 0.025


def cost(vrows, trows, height):
    vt = combine(vrows)
    tt = combine(trows)

    s = similarity(vt, tt)

    vc = (
        min(r["top"] for r in vrows)
        + max(r["bottom"] for r in vrows)
    ) / 2

    tc = (
        min(r["top"] for r in trows)
        + max(r["bottom"] for r in trows)
    ) / 2

    dy = abs(vc - tc) / height

    c = (1 - s) * 2.4 + dy * 8

    if len(vrows) != 1 or len(trows) != 1:
        c += 0.05

    return c


def align(v, t, height):
    n, m = len(v), len(t)
    inf = 10**9

    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    prev = [[None] * (m + 1) for _ in range(n + 1)]

    dp[0][0] = 0
    gap = 1.15

    for i in range(n + 1):
        for j in range(m + 1):
            cur = dp[i][j]

            if cur >= inf:
                continue

            options = []

            if i < n and j < m:
                options.append(
                    (i + 1, j + 1, 1, 1,
                     cost([v[i]], [t[j]], height))
                )

            if i + 1 < n and j < m:
                vr = v[i:i + 2]

                if close_pair(vr, height):
                    options.append(
                        (i + 2, j + 1, 2, 1,
                         cost(vr, [t[j]], height))
                    )

            if i < n and j + 1 < m:
                tr = t[j:j + 2]

                if close_pair(tr, height):
                    options.append(
                        (i + 1, j + 2, 1, 2,
                         cost([v[i]], tr, height))
                    )

            if i < n:
                options.append(
                    (i + 1, j, 1, 0, gap)
                )

            if j < m:
                options.append(
                    (i, j + 1, 0, 1, gap)
                )

            for ni, nj, nv, nt, add in options:
                value = cur + add

                if value < dp[ni][nj]:
                    dp[ni][nj] = value
                    prev[ni][nj] = (i, j, nv, nt)

    result = []
    i, j = n, m

    while i or j:
        p = prev[i][j]

        if p is None:
            raise RuntimeError(f"traceback failed: {i},{j}")

        pi, pj, nv, nt = p

        result.append({
            "vision": v[pi:pi + nv],
            "tesseract": t[pj:pj + nt],
        })

        i, j = pi, pj

    result.reverse()
    return result


def avg_v(rows):
    values = [
        r["confidence"]
        for r in rows
        if r.get("confidence") is not None
    ]

    return sum(values) / len(values) if values else 0


def avg_t(rows):
    values = [
        r["confidence"] / 100
        for r in rows
    ]

    return sum(values) / len(values) if values else 0


def obvious_garbage_score(text):
    score = 0
    words = norm(text).split()

    for w in words:
        stripped = re.sub(r"[^A-Za-zА-Яа-яЁё0-9-]", "", w)

        if not stripped:
            continue

        # suspicious ALL-CAPS OCR garbage in normal prose
        if (
            len(stripped) >= 4
            and stripped.isupper()
            and not stripped.isdigit()
            and stripped not in {"СПИДА", "ЛСД"}
        ):
            score += 2

        # malformed first-character punctuation
        if w[:1] in {"‚", "„", "{"}:
            score += 1

    return score


def resolve(vrows, trows):
    if not vrows:
        return {
            "status": "TESS_ONLY",
            "winner": "tesseract",
            "text": combine(trows),
            "similarity": 0,
        }

    if not trows:
        return {
            "status": "VISION_ONLY",
            "winner": "vision",
            "text": combine(vrows),
            "similarity": 0,
        }

    vt = combine(vrows)
    tt = combine(trows)

    s = similarity(vt, tt)
    vc = avg_v(vrows)
    tc = avg_t(trows)

    vg = obvious_garbage_score(vt)
    tg = obvious_garbage_score(tt)

    # Exact/nearly exact content: Vision is structural base.
    if s >= 0.985:
        return {
            "status": "AGREE",
            "winner": "vision",
            "text": vt,
            "similarity": s,
        }

    # One OCR contains obvious garbage and the other does not.
    if vg > tg:
        return {
            "status": "AUTO_TESS",
            "winner": "tesseract",
            "text": tt,
            "similarity": s,
        }

    if tg > vg:
        return {
            "status": "AUTO_VISION",
            "winner": "vision",
            "text": vt,
            "similarity": s,
        }

    # Extremely low Vision confidence + strong Tesseract.
    if vc < 0.40 and tc >= 0.85 and s >= 0.75:
        return {
            "status": "AUTO_TESS",
            "winner": "tesseract",
            "text": tt,
            "similarity": s,
        }

    # Weak Tesseract and usable Vision.
    if tc < 0.70 and vc >= 0.55:
        return {
            "status": "AUTO_VISION",
            "winner": "vision",
            "text": vt,
            "similarity": s,
        }

    # Very small difference, but neither engine has decisive evidence.
    # Keep Vision as base but flag for review.
    if s >= 0.94:
        return {
            "status": "REVIEW_SMALL",
            "winner": "vision",
            "text": vt,
            "similarity": s,
        }

    return {
        "status": "REVIEW",
        "winner": "vision",
        "text": vt,
        "similarity": s,
    }


def diff_words(a, b):
    aw = norm(a).split()
    bw = norm(b).split()

    matcher = SequenceMatcher(
        None,
        [w.lower() for w in aw],
        [w.lower() for w in bw],
    )

    out = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        aa = " ".join(aw[i1:i2]) or "∅"
        bb = " ".join(bw[j1:j2]) or "∅"

        out.append({
            "vision": aa,
            "tesseract": bb,
        })

    return out


def main():
    if len(sys.argv) != 6:
        print(
            "Usage: hybrid_resolve.py "
            "<vision.json> <tesseract.tsv> "
            "<width> <height> <output-prefix>"
        )
        raise SystemExit(2)

    vpath, tpath = sys.argv[1], sys.argv[2]
    width, height = int(sys.argv[3]), int(sys.argv[4]
    )
    prefix = sys.argv[5]

    v = vision_lines(vpath, height)
    t = tesseract_lines(tpath)

    units = align(v, t, height)

    resolved = []
    counts = {}

    for idx, unit in enumerate(units, 1):
        vr = unit["vision"]
        tr = unit["tesseract"]

        result = resolve(vr, tr)
        status = result["status"]

        counts[status] = counts.get(status, 0) + 1

        vt = combine(vr)
        tt = combine(tr)

        resolved.append({
            "unit": idx,
            "status": status,
            "winner": result["winner"],
            "text": result["text"],
            "vision": vt,
            "tesseract": tt,
            "vision_confidence": avg_v(vr) if vr else None,
            "tesseract_confidence": avg_t(tr) if tr else None,
            "similarity": result["similarity"],
            "differences": diff_words(vt, tt) if vr and tr else [],
            "vision_lines": [r["number"] for r in vr],
            "tesseract_lines": [r["number"] for r in tr],
        })

    json_path = prefix + ".json"
    text_path = prefix + ".txt"
    review_path = prefix + "_review.txt"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "engine": "booktext-hybrid-v3",
                "image_width": width,
                "image_height": height,
                "units": resolved,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    with open(text_path, "w", encoding="utf-8") as f:
        for r in resolved:
            f.write(r["text"])
            f.write("\n")

    review = [
        r for r in resolved
        if r["status"].startswith("REVIEW")
    ]

    with open(review_path, "w", encoding="utf-8") as f:
        for r in review:
            f.write(
                f"===== UNIT {r['unit']} "
                f"{r['status']} =====\n"
            )
            f.write(
                f"Similarity: {r['similarity']:.4f}\n"
            )
            f.write(
                "Vision confidence: "
                f"{r['vision_confidence']}\n"
            )
            f.write(
                "Tesseract confidence: "
                f"{r['tesseract_confidence']}\n"
            )
            f.write(f"VISION:    {r['vision']}\n")
            f.write(f"TESSERACT: {r['tesseract']}\n")

            if r["differences"]:
                f.write("DIFF:\n")

                for d in r["differences"]:
                    f.write(
                        f"  {d['vision']}  <=>  "
                        f"{d['tesseract']}\n"
                    )

            f.write("\n")

    print("===== HYBRID RESOLVER V3 =====")
    print("Vision lines:   ", len(v))
    print("Tesseract lines:", len(t))
    print("Alignment units:", len(units))

    print()
    print("===== DECISIONS =====")

    for key in [
        "AGREE",
        "AUTO_VISION",
        "AUTO_TESS",
        "REVIEW_SMALL",
        "REVIEW",
        "VISION_ONLY",
        "TESS_ONLY",
    ]:
        print(f"{key:15s}: {counts.get(key, 0)}")

    print()
    print("===== AUTOMATIC CHOICES =====")

    for r in resolved:
        if r["status"] not in {
            "AUTO_VISION",
            "AUTO_TESS",
        }:
            continue

        print()
        print(
            f"UNIT {r['unit']:02d} "
            f"{r['status']} "
            f"similarity={r['similarity']:.3f}"
        )
        print("VISION:   ", r["vision"])
        print("TESS:     ", r["tesseract"])
        print("CHOSEN:   ", r["text"])

    print()
    print("===== NEEDS REVIEW =====")

    for r in review:
        print()
        print(
            f"UNIT {r['unit']:02d} "
            f"{r['status']} "
            f"similarity={r['similarity']:.3f}"
        )
        print("VISION:   ", r["vision"])
        print("TESS:     ", r["tesseract"])

        for d in r["differences"]:
            print(
                "DIFF:      "
                f"{d['vision']}  <=>  "
                f"{d['tesseract']}"
            )

    print()
    print("===== OUTPUT =====")
    print("Hybrid text:", text_path)
    print("Audit JSON: ", json_path)
    print("Review:     ", review_path)


if __name__ == "__main__":
    main()
