#!/usr/bin/env python3
try:
    from .diagnostic_artifacts import diagnostic_path
except ImportError:
    from diagnostic_artifacts import diagnostic_path

# Evidence retention is explicitly opted into before tools/models run.
diagnostic_path("")


import base64
import csv
import json
import re
import subprocess
import sys
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "gemma3:4b"


def norm(s):
    return (
        s.lower()
        .replace("ё", "е")
        .replace("–", "-")
        .replace("—", "-")
        .strip()
    )


def similarity(a, b):
    return SequenceMatcher(
        None,
        norm(a),
        norm(b),
    ).ratio()


def vision_box(word, W, H):
    b = word["bbox"]

    x1 = float(b["x"]) * W
    x2 = (
        float(b["x"])
        + float(b["width"])
    ) * W

    y1 = (
        1.0
        - (
            float(b["y"])
            + float(b["height"])
        )
    ) * H

    y2 = (
        1.0
        - float(b["y"])
    ) * H

    return x1, y1, x2, y2


def overlap(a1, a2, b1, b2):
    inter = max(
        0,
        min(a2, b2)
        - max(a1, b1),
    )

    den = max(
        1,
        min(
            a2 - a1,
            b2 - b1,
        ),
    )

    return inter / den


def center(box):
    return (
        (box[0] + box[2]) / 2,
        (box[1] + box[3]) / 2,
    )


def load_tess(path):
    out = []

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
                continue

            if conf < 0:
                continue

            x = float(row["left"])
            y = float(row["top"])
            w = float(row["width"])
            h = float(row["height"])

            out.append(
                {
                    "text": text,
                    "confidence": conf / 100,
                    "bbox": (
                        x,
                        y,
                        x + w,
                        y + h,
                    ),
                }
            )

    return out


def score_pair(v, t):

    vb = v["bbox"]
    tb = t["bbox"]

    vx, vy = center(vb)
    tx, ty = center(tb)

    vh = max(
        1,
        vb[3] - vb[1],
    )

    th = max(
        1,
        tb[3] - tb[1],
    )

    avg_h = (
        vh + th
    ) / 2

    dy = abs(
        vy - ty
    ) / avg_h

    xo = overlap(
        vb[0],
        vb[2],
        tb[0],
        tb[2],
    )

    yo = overlap(
        vb[1],
        vb[3],
        tb[1],
        tb[3],
    )

    s = similarity(
        v["text"],
        t["text"],
    )

    return (
        yo * 4
        + xo * 2
        + s * 1.2
        - dy * 1.5
    )


def match_words(V, T):

    pairs = []

    for vi, v in enumerate(V):
        for ti, t in enumerate(T):

            _, vy = center(
                v["bbox"]
            )

            _, ty = center(
                t["bbox"]
            )

            if abs(vy - ty) > 15:
                continue

            score = score_pair(
                v,
                t,
            )

            if score >= 1:
                pairs.append(
                    (
                        score,
                        vi,
                        ti,
                    )
                )

    pairs.sort(
        reverse=True
    )

    used_v = set()
    used_t = set()
    matches = {}

    for score, vi, ti in pairs:

        if (
            vi in used_v
            or ti in used_t
        ):
            continue

        used_v.add(vi)
        used_t.add(ti)

        matches[vi] = ti

    return matches, used_t


def crop_word(
    page,
    box,
    destination,
    W,
    H,
):

    x1, y1, x2, y2 = box

    # More horizontal context than V4/V5,
    # but still a tightly controlled OCR crop.
    padx = max(
        35,
        int(
            (x2 - x1) * 0.65
        ),
    )

    pady = max(
        15,
        int(
            (y2 - y1) * 1.4
        ),
    )

    left = max(
        0,
        int(x1 - padx),
    )

    right = min(
        W,
        int(x2 + padx),
    )

    top = max(
        0,
        int(y1 - pady),
    )

    bottom = min(
        H,
        int(y2 + pady),
    )

    subprocess.run(
        [
            "magick",
            page,
            "-crop",
            (
                f"{right-left}x"
                f"{bottom-top}+"
                f"{left}+{top}"
            ),
            "+repage",
            "-resize",
            "500%",
            "-colorspace",
            "Gray",
            "-sharpen",
            "0x1",
            destination,
        ],
        check=True,
    )


def gemma_word(
    image,
    vision,
    tess,
    previous,
    following,
):

    data64 = base64.b64encode(
        Path(image).read_bytes()
    ).decode("ascii")

    prompt = f"""
Ты третий OCR-движок.

Нужно определить ОДНО спорное печатное слово
по изображению.

Apple Vision:
{vision}

Tesseract:
{tess}

Предыдущее слово:
{previous}

Следующее слово:
{following}

Правила:
- главным доказательством является изображение;
- верни только спорное слово;
- не переписывай предложение;
- не исправляй книгу литературно;
- сохраняй пунктуацию, если она относится к слову;
- сохраняй конечный дефис;
- если видишь вариант, которого нет среди двух OCR,
  можешь вернуть его;
- JSON:
{{"word":"..."}}
""".strip()

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "images": [data64],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(
            payload
        ).encode(),
        headers={
            "Content-Type":
            "application/json"
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=600,
    ) as response:

        result = json.loads(
            response.read()
        )

    raw = result.get(
        "response",
        "",
    ).strip()

    try:
        return json.loads(
            raw
        ).get(
            "word",
            "",
        ).strip()

    except Exception:
        return raw


def deterministic(v, t):

    a = v["text"]
    b = t["text"]

    if norm(a) == norm(b):
        return (
            "AGREE",
            a,
        )

    s = similarity(
        a,
        b,
    )

    vc = (
        v["confidence"]
        if v["confidence"]
        is not None
        else 0
    )

    tc = t["confidence"]

    # Strong OCR corruption patterns.
    if (
        a.startswith(
            ("‚", "{")
        )
        and not b.startswith(
            ("‚", "{")
        )
    ):
        return (
            "TESS",
            b,
        )

    if (
        b.startswith(
            ("‚", "{")
        )
        and not a.startswith(
            ("‚", "{")
        )
    ):
        return (
            "VISION",
            a,
        )

    # Extremely low Vision confidence +
    # strong Tesseract and substantial agreement.
    if (
        vc < 0.35
        and tc > 0.85
        and s > 0.70
    ):
        return (
            "TESS",
            b,
        )

    # Extremely low Tesseract confidence +
    # reasonable Vision.
    if (
        tc < 0.30
        and vc > 0.50
        and s > 0.70
    ):
        return (
            "VISION",
            a,
        )

    # Otherwise deliberately do NOT guess.
    return (
        "AMBIGUOUS",
        None,
    )


def main():

    if len(sys.argv) != 6:
        print(
            "Usage: resolver "
            "<page> <vision.json> "
            "<tess.tsv> <W> <H>"
        )
        raise SystemExit(2)

    page = sys.argv[1]
    vision_path = sys.argv[2]
    tess_path = sys.argv[3]

    W = int(sys.argv[4])
    H = int(sys.argv[5])

    with open(
        vision_path,
        encoding="utf-8",
    ) as f:
        vision = json.load(f)

    V = []

    for w in vision["words"]:
        V.append(
            {
                "text": w["text"],
                "confidence":
                    w.get("confidence"),
                "bbox":
                    vision_box(
                        w,
                        W,
                        H,
                    ),
            }
        )

    T = load_tess(
        tess_path
    )

    matches, used_t = (
        match_words(
            V,
            T,
        )
    )

    cropdir = Path(
        str(diagnostic_path("booktext-v7-crops"))
    )

    cropdir.mkdir(
        parents=True,
        exist_ok=True,
    )

    results = []

    print(
        "===== HYBRID RESOLVER V7 ====="
    )

    print(
        "Vision words:",
        len(V),
    )

    print(
        "Tesseract words:",
        len(T),
    )

    print()

    counters = {
        "AGREE": 0,
        "VISION": 0,
        "TESS": 0,
        "GEMMA": 0,
        "UNMATCHED": 0,
    }

    for vi, v in enumerate(V):

        ti = matches.get(vi)

        if ti is None:

            counters[
                "UNMATCHED"
            ] += 1

            results.append(
                {
                    "vision_index": vi,
                    "vision": v["text"],
                    "final": v["text"],
                    "source":
                        "VISION_UNMATCHED",
                }
            )

            continue

        t = T[ti]

        decision, chosen = (
            deterministic(
                v,
                t,
            )
        )

        gemma = None

        if decision == "AMBIGUOUS":

            crop = (
                cropdir
                / f"word-{vi+1:03d}.png"
            )

            crop_word(
                page,
                v["bbox"],
                str(crop),
                W,
                H,
            )

            previous = (
                V[vi - 1]["text"]
                if vi > 0
                else ""
            )

            following = (
                V[vi + 1]["text"]
                if vi + 1 < len(V)
                else ""
            )

            try:
                gemma = gemma_word(
                    str(crop),
                    v["text"],
                    t["text"],
                    previous,
                    following,
                )
            except Exception as exc:
                gemma = ""

            if gemma:
                chosen = gemma
                decision = "GEMMA"
            else:
                chosen = v["text"]
                decision = "VISION"

        counters[
            decision
        ] = counters.get(
            decision,
            0,
        ) + 1

        record = {
            "vision_index": vi,
            "tesseract_index": ti,
            "vision": v["text"],
            "vision_confidence":
                v["confidence"],
            "tesseract": t["text"],
            "tesseract_confidence":
                t["confidence"],
            "gemma": gemma,
            "final": chosen,
            "source": decision,
        }

        results.append(
            record
        )

        if norm(
            v["text"]
        ) != norm(
            t["text"]
        ):

            print(
                f"===== WORD {vi+1:03d} ====="
            )

            print(
                "VISION:",
                v["text"],
                f"({v['confidence']:.3f})",
            )

            print(
                "TESS:  ",
                t["text"],
                f"({t['confidence']:.3f})",
            )

            if gemma is not None:
                print(
                    "GEMMA: ",
                    gemma,
                )

            print(
                "FINAL: ",
                chosen,
            )

            print(
                "SOURCE:",
                decision,
            )

            print()

    out = (
        str(diagnostic_path("booktext-hybrid-v7.json"))
    )

    with open(
        out,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            {
                "engine":
                    "booktext-hybrid-v7",
                "counts":
                    counters,
                "words":
                    results,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        "===== COUNTS ====="
    )

    for k, v in counters.items():
        print(
            f"{k:10s}: {v}"
        )

    print()
    print(
        "Audit:",
        out,
    )

    print(
        "Crops:",
        cropdir,
    )


if __name__ == "__main__":
    main()
