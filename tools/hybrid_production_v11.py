#!/usr/bin/env python3
try:
    from .diagnostic_artifacts import diagnostic_path
except ImportError:
    from diagnostic_artifacts import diagnostic_path

# Evidence retention is explicitly opted into before tools/models run.
diagnostic_path("")


import json
import re
from pathlib import Path

ALIGN = Path(str(diagnostic_path("booktext-hybrid-v10-alignment.json")))
V7 = Path(str(diagnostic_path("booktext-hybrid-v7.json")))

OUT_JSON = Path(str(diagnostic_path("booktext-hybrid-v11.json")))
OUT_REVIEW = Path(str(diagnostic_path("booktext-hybrid-v11-review.txt")))
OUT_TEXT = Path(str(diagnostic_path("booktext-hybrid-v11.txt")))


def norm(s):
    return (
        (s or "")
        .lower()
        .replace("ё", "е")
        .replace("–", "-")
        .replace("—", "-")
        .strip()
    )


def lexical(s):
    return re.sub(
        r"^[^\wА-Яа-яЁё]+|[^\wА-Яа-яЁё-]+$",
        "",
        norm(s),
    )


def strange_leading(s):
    return bool(
        s and s[0] in {
            "‚", "{", "}", "„", "“", "”",
        }
    )


def latin_garbage(s):
    letters = re.sub(
        r"[^A-Za-zА-Яа-яЁё]",
        "",
        s or "",
    )

    if not letters:
        return False

    latin = sum(
        ("a" <= c.lower() <= "z")
        for c in letters
    )

    cyr = sum(
        ("А" <= c <= "я") or c in "Ёё"
        for c in letters
    )

    return latin >= 2 and latin >= cyr


def safe_11(v, t):
    a = v["text"]
    b = t["text"]

    vc = float(v.get("confidence") or 0)
    tc = float(t.get("confidence") or 0)

    if norm(a) == norm(b):
        return (
            "AGREE",
            a,
            "Vision and Tesseract agree",
        )

    # Obviously corrupted opening glyph.
    if strange_leading(a) and not strange_leading(b):
        return (
            "AUTO_TESS",
            b,
            "Vision has abnormal leading glyph",
        )

    if strange_leading(b) and not strange_leading(a):
        return (
            "AUTO_VISION",
            a,
            "Tesseract has abnormal leading glyph",
        )

    # Obvious Latin garbage on this Russian page.
    if latin_garbage(a) and not latin_garbage(b):
        return (
            "AUTO_TESS",
            b,
            "Vision contains Latin OCR garbage",
        )

    if latin_garbage(b) and not latin_garbage(a):
        return (
            "AUTO_VISION",
            a,
            "Tesseract contains Latin OCR garbage",
        )

    # Same lexical word; punctuation only differs.
    if lexical(a) and lexical(a) == lexical(b):
        return (
            "AUTO_VISION_PUNCT",
            a,
            "Lexical token agrees; punctuation differs",
        )

    # Very weak Vision + very strong Tesseract,
    # only when the token lengths agree.
    #
    # Examples on this page:
    #   витамив -> витамин
    #   из      -> их
    #
    # We deliberately do NOT use this rule for
    # substantially different words.
    if (
        vc < 0.35
        and tc > 0.90
        and len(lexical(a)) == len(lexical(b))
        and lexical(a)
        and lexical(b)
    ):
        return (
            "AUTO_TESS_LOWVISION",
            b,
            "Very low Vision confidence and strong Tesseract on equal-length token",
        )

    return (
        "REVIEW",
        a,
        "No safe deterministic winner",
    )


alignment = json.loads(
    ALIGN.read_text(encoding="utf-8")
)

gemma_by_vindex = {}

if V7.exists():
    v7 = json.loads(
        V7.read_text(encoding="utf-8")
    )

    for r in v7.get("words", []):
        vi = r.get("vision_index")

        if vi is not None:
            # V7 index is zero-based.
            gemma_by_vindex[int(vi) + 1] = r.get("gemma")


units = alignment["units"]

resolved = []
counts = {}
review = []


def add_count(status):
    counts[status] = counts.get(status, 0) + 1


for unit_no, u in enumerate(units, 1):

    kind = u["kind"]
    V = u.get("vision", [])
    T = u.get("tesseract", [])

    if kind == "1:1":

        v = V[0]
        t = T[0]

        status, final, reason = safe_11(v, t)

        record = {
            "unit": unit_no,
            "kind": kind,

            "vision_index": v["index"],
            "vision": v["text"],
            "vision_confidence": v.get("confidence"),

            "tesseract_index": t["index"],
            "tesseract": t["text"],
            "tesseract_confidence": t.get("confidence"),

            "gemma_signal": gemma_by_vindex.get(
                v["index"]
            ),

            "status": status,
            "final_tokens": [final],
            "reason": reason,
        }

        resolved.append(record)
        add_count(status)

    elif kind == "T_ONLY":

        t = T[0]

        record = {
            "unit": unit_no,
            "kind": kind,

            "vision": None,

            "tesseract_index": t["index"],
            "tesseract": t["text"],
            "tesseract_confidence": t.get("confidence"),

            "gemma_signal": None,

            "status": "AUTO_TESS_INSERT",
            "final_tokens": [t["text"]],
            "reason": (
                "Sequential alignment identifies a genuine "
                "word omitted by Vision"
            ),
        }

        resolved.append(record)
        add_count("AUTO_TESS_INSERT")

    elif kind == "V_ONLY":

        v = V[0]

        record = {
            "unit": unit_no,
            "kind": kind,

            "vision_index": v["index"],
            "vision": v["text"],
            "vision_confidence": v.get("confidence"),

            "tesseract": None,

            "gemma_signal": gemma_by_vindex.get(
                v["index"]
            ),

            "status": "AUTO_VISION_INSERT",
            "final_tokens": [v["text"]],
            "reason": (
                "Sequential alignment identifies a genuine "
                "word omitted by Tesseract"
            ),
        }

        resolved.append(record)
        add_count("AUTO_VISION_INSERT")

    elif kind in {"1:2", "1:3"}:

        # V10 permits these groups only when the grouped
        # Tesseract tokens materially explain a split OCR.
        #
        # On page 181:
        #   обнаружила <=> о + ружила
        #
        # Vision preserves the actual single printed word.
        v = V[0]

        tess_text = " ".join(
            x["text"] for x in T
        )

        record = {
            "unit": unit_no,
            "kind": kind,

            "vision_index": v["index"],
            "vision": v["text"],
            "vision_confidence": v.get("confidence"),

            "tesseract": tess_text,
            "tesseract_parts": T,

            "gemma_signal": gemma_by_vindex.get(
                v["index"]
            ),

            "status": "AUTO_VISION_SPLIT",
            "final_tokens": [v["text"]],
            "reason": (
                "Tesseract split one printed Vision word "
                "into multiple fragments"
            ),
        }

        resolved.append(record)
        add_count("AUTO_VISION_SPLIT")

    elif kind in {"2:1", "3:1"}:

        # Symmetric case: Vision split one printed token.
        # Preserve Tesseract single-token reading but keep audit.
        t = T[0]

        vision_text = " ".join(
            x["text"] for x in V
        )

        record = {
            "unit": unit_no,
            "kind": kind,

            "vision": vision_text,
            "vision_parts": V,

            "tesseract_index": t["index"],
            "tesseract": t["text"],
            "tesseract_confidence": t.get("confidence"),

            "gemma_signal": None,

            "status": "AUTO_TESS_SPLIT",
            "final_tokens": [t["text"]],
            "reason": (
                "Vision split one printed Tesseract word "
                "into multiple fragments"
            ),
        }

        resolved.append(record)
        add_count("AUTO_TESS_SPLIT")

    else:
        raise RuntimeError(
            f"Unsupported alignment kind: {kind}"
        )


# Build context after all units are resolved.
flat_tokens = []

for r in resolved:
    for token in r["final_tokens"]:
        flat_tokens.append(
            (r["unit"], token)
        )


for idx, r in enumerate(resolved):

    if r["status"] != "REVIEW":
        continue

    before = []
    after = []

    for rr in resolved[max(0, idx-4):idx]:
        before.extend(rr["final_tokens"])

    for rr in resolved[idx+1:idx+5]:
        after.extend(rr["final_tokens"])

    current = r["vision"] or r["tesseract"] or ""

    context = (
        " ".join(before[-8:])
        + " <<<"
        + current
        + ">>> "
        + " ".join(after[:8])
    ).strip()

    r["context"] = context
    review.append(r)


# Plain-text prototype.
# This intentionally preserves token order but not yet Vision
# paragraph/line structure. Layout reconstruction comes at
# integration stage.
draft_tokens = []

for r in resolved:
    draft_tokens.extend(
        r["final_tokens"]
    )

OUT_TEXT.write_text(
    " ".join(draft_tokens),
    encoding="utf-8",
)


OUT_JSON.write_text(
    json.dumps(
        {
            "engine": "booktext-hybrid-production-v11",

            "policy": {
                "alignment": (
                    "V10 sequential geometric/text alignment"
                ),
                "gemma": (
                    "audit signal only for normal OCR; "
                    "never automatically changes text"
                ),
                "review": (
                    "Vision reading retained in draft until "
                    "a later constrained adjudication layer"
                ),
            },

            "counts": counts,
            "alignment_units": len(units),
            "resolved_units": resolved,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)


with OUT_REVIEW.open(
    "w",
    encoding="utf-8",
) as f:

    f.write(
        "BOOKTEXT HYBRID V11 — REVIEW\n"
    )
    f.write("=" * 70 + "\n\n")

    for r in review:

        f.write(
            f"UNIT {r['unit']:03d}\n"
        )

        f.write(
            f"CONTEXT: {r['context']}\n"
        )

        f.write(
            f"VISION: {r.get('vision')} "
            f"[{r.get('vision_confidence')}]\n"
        )

        f.write(
            f"TESS:   {r.get('tesseract')} "
            f"[{r.get('tesseract_confidence')}]\n"
        )

        f.write(
            f"GEMMA SIGNAL: "
            f"{r.get('gemma_signal')}\n"
        )

        f.write(
            "DRAFT:  "
            + " ".join(r["final_tokens"])
            + "\n"
        )

        f.write(
            f"REASON: {r['reason']}\n"
        )

        f.write("-" * 70 + "\n\n")


print("===== BOOKTEXT HYBRID PRODUCTION V11 =====")

print("Alignment units:", len(units))

print()
print("===== DECISIONS =====")

for key in sorted(counts):
    print(
        f"{key:24s}: {counts[key]}"
    )

print()
print("Needs review:", len(review))

print()
print("===== STRUCTURAL SPECIAL CASES =====")

for r in resolved:

    if r["kind"] == "1:1":
        continue

    print()
    print(
        f"UNIT {r['unit']:03d} "
        f"{r['kind']} "
        f"=> {r['status']}"
    )

    print(
        "VISION:",
        r.get("vision") or "∅"
    )

    print(
        "TESS:  ",
        r.get("tesseract") or "∅"
    )

    print(
        "FINAL: ",
        " ".join(r["final_tokens"])
    )


print()
print("===== REVIEW CASES =====")

for r in review:

    print()
    print(
        f"UNIT {r['unit']:03d}"
    )

    print(
        "CONTEXT:",
        r["context"]
    )

    print(
        "VISION:",
        r.get("vision"),
        f"({r.get('vision_confidence')})"
    )

    print(
        "TESS:  ",
        r.get("tesseract"),
        f"({r.get('tesseract_confidence')})"
    )

    print(
        "GEMMA: ",
        r.get("gemma_signal")
    )


print()
print("===== KEY TEXT CHECKS =====")

draft = OUT_TEXT.read_text(
    encoding="utf-8"
)

checks = [
    "делать лапшу",
    "витамин С в макароны",
    "Я обнаружила следы эрготоксинов",
]

for c in checks:
    print(
        ("PASS" if c in draft else "MISS"),
        "|",
        c
    )


print()
print("===== OUTPUT =====")
print("TEXT:  ", OUT_TEXT)
print("JSON:  ", OUT_JSON)
print("REVIEW:", OUT_REVIEW)
