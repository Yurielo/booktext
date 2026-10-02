#!/usr/bin/env python3
try:
    from .diagnostic_artifacts import diagnostic_path
except ImportError:
    from diagnostic_artifacts import diagnostic_path

# Evidence retention is explicitly opted into before tools/models run.
diagnostic_path("")


import json
import subprocess
from pathlib import Path

SRC = Path(str(diagnostic_path("booktext-hybrid-v11.json")))
SPELL = Path.home() / "AITools/booktext/bin/booktext-spellcheck"

OUT_JSON = Path(str(diagnostic_path("booktext-hybrid-v12.json")))
OUT_TEXT = Path(str(diagnostic_path("booktext-hybrid-v12.txt")))
OUT_REVIEW = Path(str(diagnostic_path("booktext-hybrid-v12-review.txt")))

data = json.loads(SRC.read_text(encoding="utf-8"))
records = data["resolved_units"]


def spell_pair(a, b):
    p = subprocess.run(
        [str(SPELL), a or "", b or ""],
        capture_output=True,
        text=True,
        check=True,
    )

    lines = [
        x.strip()
        for x in p.stdout.splitlines()
        if x.strip()
    ]

    if len(lines) != 2:
        return "UNKNOWN", "UNKNOWN"

    return lines[0], lines[1]


counts = {}
review = []


def count(status):
    counts[status] = counts.get(status, 0) + 1


for r in records:

    old_status = r["status"]

    if old_status != "REVIEW":
        count(old_status)
        continue

    v = r.get("vision") or ""
    t = r.get("tesseract") or ""

    vs, ts = spell_pair(v, t)

    r["spellcheck"] = {
        "vision": vs,
        "tesseract": ts,
    }

    if vs == "VALID" and ts == "INVALID":
        r["status"] = "AUTO_VISION_SPELL"
        r["final_tokens"] = [v]
        r["reason"] = (
            "Vision token is valid in macOS Russian dictionary; "
            "Tesseract token is invalid"
        )

    elif ts == "VALID" and vs == "INVALID":
        r["status"] = "AUTO_TESS_SPELL"
        r["final_tokens"] = [t]
        r["reason"] = (
            "Tesseract token is valid in macOS Russian dictionary; "
            "Vision token is invalid"
        )

    else:
        r["status"] = "REVIEW"
        r["reason"] = (
            "Spell checker cannot safely distinguish OCR variants"
        )
        review.append(r)

    count(r["status"])


tokens = []

for r in records:
    tokens.extend(r.get("final_tokens", []))

draft = " ".join(tokens)

OUT_TEXT.write_text(
    draft,
    encoding="utf-8",
)

OUT_JSON.write_text(
    json.dumps(
        {
            "engine": "booktext-hybrid-production-v12",
            "policy": {
                "alignment": "V10",
                "base_resolver": "V11",
                "spellcheck": (
                    "macOS NSSpellChecker ru_RU; automatic only "
                    "when exactly one OCR candidate is VALID"
                ),
                "gemma": (
                    "audit signal only; never automatically changes OCR"
                ),
            },
            "counts": counts,
            "resolved_units": records,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)


with OUT_REVIEW.open("w", encoding="utf-8") as f:

    f.write("BOOKTEXT HYBRID V12 — UNRESOLVED REVIEW\n")
    f.write("=" * 72 + "\n\n")

    for r in review:
        f.write(f"UNIT {r['unit']:03d}\n")
        f.write(f"CONTEXT: {r.get('context','')}\n")
        f.write(
            f"VISION: {r.get('vision')} "
            f"[{r.get('vision_confidence')}] "
            f"SPELL={r['spellcheck']['vision']}\n"
        )
        f.write(
            f"TESS:   {r.get('tesseract')} "
            f"[{r.get('tesseract_confidence')}] "
            f"SPELL={r['spellcheck']['tesseract']}\n"
        )
        f.write(
            f"GEMMA:  {r.get('gemma_signal')}\n"
        )
        f.write("-" * 72 + "\n\n")


print("===== BOOKTEXT HYBRID PRODUCTION V12 =====")

print()
print("===== DECISIONS =====")

for key in sorted(counts):
    print(f"{key:24s}: {counts[key]}")

print()
print("Needs review:", len(review))

print()
print("===== REMAINING REVIEW =====")

for r in review:
    print()
    print(f"UNIT {r['unit']:03d}")
    print("CONTEXT:", r.get("context", ""))
    print(
        "VISION:",
        r.get("vision"),
        f"({r.get('vision_confidence')})",
        "SPELL=" + r["spellcheck"]["vision"],
    )
    print(
        "TESS:  ",
        r.get("tesseract"),
        f"({r.get('tesseract_confidence')})",
        "SPELL=" + r["spellcheck"]["tesseract"],
    )
    print("GEMMA: ", r.get("gemma_signal"))

print()
print("===== KEY TEXT CHECKS =====")

checks = [
    "воздействие.",
    "иммунитет",
    "каждого,",
    "консервированной",
    "делать лапшу",
    "ядовиты",
    "Я обнаружила следы эрготоксинов",
    "витамина С",
    "это требует большего времени",
    "деревянной или пластиковой ложкой.",
    "поведение,",
    "зараженных",
    "витамин С в макароны",
]

for phrase in checks:
    print(
        "PASS" if phrase in draft else "MISS",
        "|",
        phrase,
    )

print()
print("===== OUTPUT =====")
print("TEXT:  ", OUT_TEXT)
print("JSON:  ", OUT_JSON)
print("REVIEW:", OUT_REVIEW)
