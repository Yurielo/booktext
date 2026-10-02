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

SRC = Path(str(diagnostic_path("booktext-hybrid-v7.json")))
OUT_JSON = Path(str(diagnostic_path("booktext-hybrid-production.json")))
OUT_REVIEW = Path(str(diagnostic_path("booktext-hybrid-production-review.txt")))
OUT_DRAFT = Path(str(diagnostic_path("booktext-hybrid-production-draft.txt")))


def norm(s):
    return (
        (s or "")
        .lower()
        .replace("ё", "е")
        .replace("–", "-")
        .replace("—", "-")
        .strip()
    )


def strip_punct(s):
    return re.sub(
        r"^[^\wА-Яа-яЁё]+|[^\wА-Яа-яЁё-]+$",
        "",
        norm(s),
    )


def strange_leading(s):
    return bool(
        s
        and s[0] in {
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
        "A" <= c <= "Z"
        or "a" <= c <= "z"
        for c in letters
    )

    cyr = sum(
        ("А" <= c <= "я")
        or c in "Ёё"
        for c in letters
    )

    # Pure/mixed Latin token in otherwise Russian OCR
    return latin >= 2 and latin >= cyr


def decide(v, t):
    a = v or ""
    b = t or ""

    if norm(a) == norm(b):
        return "AGREE", a, "Both OCR engines agree"

    # One OCR introduced obviously abnormal opening glyph.
    if strange_leading(a) and not strange_leading(b):
        return (
            "AUTO_TESS",
            b,
            "Vision has abnormal leading OCR glyph",
        )

    if strange_leading(b) and not strange_leading(a):
        return (
            "AUTO_VISION",
            a,
            "Tesseract has abnormal leading OCR glyph",
        )

    # One OCR produced obvious Latin garbage on Russian page.
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

    # Same lexical token, only punctuation differs.
    # Do not invent punctuation: keep Vision structural version,
    # but retain an audit record.
    if (
        strip_punct(a)
        and strip_punct(a) == strip_punct(b)
    ):
        return (
            "AUTO_VISION_PUNCT",
            a,
            "Same lexical token; punctuation differs",
        )

    # Very low Vision confidence + high Tesseract,
    # but ONLY for a near-identical token.
    # This catches e.g. витамив -> витамин without allowing
    # confidence to overrule clearly different readings.
    vc = float(v_conf or 0)
    tc = float(t_conf or 0)

    if (
        vc < 0.35
        and tc > 0.90
        and len(strip_punct(a)) == len(strip_punct(b))
    ):
        return (
            "AUTO_TESS_LOWVISION",
            b,
            "Very low Vision confidence and high Tesseract confidence on equal-length token",
        )

    return (
        "REVIEW",
        a,
        "No safe deterministic winner",
    )


with SRC.open(encoding="utf-8") as f:
    data = json.load(f)

words = data["words"]

resolved = []
review = []

counts = {}

for i, r in enumerate(words):
    v = r.get("vision", "")
    t = r.get("tesseract")

    # V7 contains agreement records too.
    if t is None:
        status = "VISION_ONLY"
        final = v
        reason = "No matched Tesseract token"
    else:
        v_conf = r.get("vision_confidence")
        t_conf = r.get("tesseract_confidence")

        # expose to decide()
        globals()["v_conf"] = v_conf
        globals()["t_conf"] = t_conf

        status, final, reason = decide(v, t)

    counts[status] = counts.get(status, 0) + 1

    record = {
        "index": i + 1,
        "vision": v,
        "vision_confidence": r.get(
            "vision_confidence"
        ),
        "tesseract": t,
        "tesseract_confidence": r.get(
            "tesseract_confidence"
        ),
        "gemma_signal": r.get("gemma"),
        "final": final,
        "status": status,
        "reason": reason,
    }

    resolved.append(record)

    if status == "REVIEW":
        start = max(0, i - 5)
        end = min(len(words), i + 6)

        context = []

        for j in range(start, end):
            token = words[j].get("vision", "")
            if j == i:
                token = f"<<<{token}>>>"
            context.append(token)

        record["context"] = " ".join(context)
        review.append(record)


OUT_JSON.write_text(
    json.dumps(
        {
            "engine": "booktext-hybrid-production-prototype",
            "policy": {
                "gemma": (
                    "audit signal only; never accepted "
                    "automatically for normal OCR"
                ),
                "review": (
                    "Vision retained as draft; conflict "
                    "preserved for later adjudication"
                ),
            },
            "counts": counts,
            "words": resolved,
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
        "BOOKTEXT HYBRID — MANUAL/THIRD-STAGE REVIEW\n"
    )
    f.write("=" * 60 + "\n\n")

    for r in review:
        f.write(
            f"WORD {r['index']:03d}\n"
        )
        f.write(
            f"CONTEXT: {r['context']}\n"
        )
        f.write(
            f"VISION: {r['vision']} "
            f"[{r['vision_confidence']}]\n"
        )
        f.write(
            f"TESS:   {r['tesseract']} "
            f"[{r['tesseract_confidence']}]\n"
        )
        f.write(
            f"GEMMA SIGNAL: "
            f"{r['gemma_signal']}\n"
        )
        f.write(
            f"DRAFT:  {r['final']}\n"
        )
        f.write(
            f"REASON: {r['reason']}\n"
        )
        f.write("-" * 60 + "\n\n")


OUT_DRAFT.write_text(
    " ".join(
        r["final"]
        for r in resolved
        if r["final"]
    ),
    encoding="utf-8",
)


print(
    "===== PRODUCTION HYBRID PROTOTYPE ====="
)

for key in sorted(counts):
    print(
        f"{key:22s}: {counts[key]}"
    )

print()
print(
    "Total words:",
    len(resolved),
)

print(
    "Needs review:",
    len(review),
)

print()
print(
    "===== REVIEW CASES ====="
)

for r in review:
    print()
    print(
        f"WORD {r['index']:03d}"
    )
    print(
        "CONTEXT:",
        r["context"],
    )
    print(
        "VISION:",
        r["vision"],
        f"({r['vision_confidence']})",
    )
    print(
        "TESS:  ",
        r["tesseract"],
        f"({r['tesseract_confidence']})",
    )
    print(
        "GEMMA: ",
        r["gemma_signal"],
    )

print()
print(
    "===== OUTPUT ====="
)
print(
    "JSON:  ",
    OUT_JSON,
)
print(
    "REVIEW:",
    OUT_REVIEW,
)
print(
    "DRAFT: ",
    OUT_DRAFT,
)
