#!/usr/bin/env python3

import json
import re
from pathlib import Path
from collections import Counter

V12 = Path("/tmp/booktext-hybrid-v12.json")
V14 = Path("/tmp/booktext-hybrid-v14-consensus-audit.json")
V18 = Path("/tmp/booktext-v18-crops/v18-results.json")

OUT_JSON = Path("/tmp/booktext-hybrid-v19-review.json")
OUT_TXT = Path("/tmp/booktext-hybrid-v19-review.txt")


def norm(s):
    s = s.lower().strip()
    s = s.replace("ё", "е")
    s = re.sub(r"[^\wа-яa-z-]+", "", s, flags=re.I)
    return s


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


v12 = load(V12)
v14 = load(V14)
v18 = load(V18)


# ---------- V18 evidence ----------

v18_by_word = {}

for rec in v18:
    readings = []

    for variant, modes in rec.get("ocr", {}).items():
        for psm, text in modes.items():
            if text:
                readings.append({
                    "variant": variant,
                    "psm": int(psm),
                    "text": text,
                })

    v18_by_word[rec["vision"]] = {
        "vision_index": rec["index"],
        "vision_confidence": rec.get("confidence"),
        "crop": rec.get("crop"),
        "readings": readings,
        "files": rec.get("files"),
    }


# ---------- V14 consensus-invalid ----------

v14_invalid = {}

for x in v14.get("invalid_tokens", []):
    v14_invalid[(x.get("unit"), x.get("token"))] = x


# ---------- final review items ----------

items = []

for unit in v12.get("resolved_units", []):
    final_tokens = unit.get("final_tokens", [])
    final = " ".join(final_tokens)
    status = unit.get("status", "")

    item = {
        "unit": unit.get("unit"),
        "status": status,
        "final": final,
        "vision": unit.get("vision"),
        "tesseract": unit.get("tesseract"),
        "vision_confidence": unit.get("vision_confidence"),
        "tesseract_confidence": unit.get("tesseract_confidence"),
        "gemma": unit.get("gemma_signal"),
        "reason": unit.get("reason"),
        "context": unit.get("context"),
        "signals": [],
    }

    # Unresolved V12 disagreement.
    if status == "REVIEW":
        item["signals"].append("V12_REVIEW")

    # Consensus token rejected by spell checker.
    key = (unit.get("unit"), final)
    if key in v14_invalid:
        item["signals"].append("SPELL_INVALID")
        item["spell"] = v14_invalid[key]

    # Attach local visual evidence if available by Vision/final token.
    candidates = [
        unit.get("vision"),
        final,
        unit.get("tesseract"),
    ]

    evidence = None
    for candidate in candidates:
        if candidate in v18_by_word:
            evidence = v18_by_word[candidate]
            break

    if evidence:
        item["signals"].append("LOCAL_REOCR")
        item["local_reocr"] = evidence

    if item["signals"]:
        items.append(item)


# Also include V14 invalids that don't map cleanly to a V12 unit.
known = {(x.get("unit"), x.get("final")) for x in items}

for x in v14.get("invalid_tokens", []):
    key = (x.get("unit"), x.get("token"))

    if key in known:
        continue

    item = {
        "unit": x.get("unit"),
        "status": x.get("status"),
        "final": x.get("token"),
        "context": x.get("context"),
        "signals": ["SPELL_INVALID"],
        "spell": x,
    }

    if x.get("token") in v18_by_word:
        item["signals"].append("LOCAL_REOCR")
        item["local_reocr"] = v18_by_word[x["token"]]

    items.append(item)


items.sort(key=lambda x: (x.get("unit") or 0, x.get("final") or ""))

result = {
    "engine": "booktext-review-queue-v19",
    "items": items,
}

OUT_JSON.write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)


lines = []

lines.append("===== BOOKTEXT REVIEW QUEUE V19 =====")
lines.append(f"Items: {len(items)}")
lines.append("")

counter = Counter()

for x in items:
    for s in x["signals"]:
        counter[s] += 1

lines.append("===== SIGNAL COUNTS =====")

for key in sorted(counter):
    lines.append(f"{key:20s}: {counter[key]}")

lines.append("")
lines.append("===== ITEMS =====")

for x in items:
    lines.append("")
    lines.append("=" * 72)
    lines.append(f"UNIT {x.get('unit'):03d}")
    lines.append(f"STATUS:  {x.get('status')}")
    lines.append(f"FINAL:   {x.get('final')}")
    lines.append(f"SIGNALS: {', '.join(x['signals'])}")

    if x.get("vision") is not None:
        lines.append(f"VISION:  {x.get('vision')}")

    if x.get("tesseract") is not None:
        lines.append(f"TESS:    {x.get('tesseract')}")

    if x.get("vision_confidence") is not None:
        lines.append(f"VCONF:   {x.get('vision_confidence'):.4f}")

    if x.get("tesseract_confidence") is not None:
        lines.append(f"TCONF:   {x.get('tesseract_confidence'):.4f}")

    if x.get("reason"):
        lines.append(f"REASON:  {x.get('reason')}")

    if x.get("gemma") is not None:
        lines.append(f"GEMMA:   {x.get('gemma')}")

    if x.get("context"):
        lines.append(f"CONTEXT: {x.get('context')}")

    local = x.get("local_reocr")

    if local:
        lines.append("LOCAL RE-OCR:")

        for r in local["readings"]:
            lines.append(
                f"  {r['variant']:5s} PSM {r['psm']:2d}: {r['text']}"
            )

OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

print("\n".join(lines))
print()
print("===== OUTPUT =====")
print(OUT_JSON)
print(OUT_TXT)
