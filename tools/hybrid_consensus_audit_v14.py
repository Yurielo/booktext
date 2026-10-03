#!/usr/bin/env python3
try:
    from .diagnostic_artifacts import diagnostic_path
except ImportError:
    from diagnostic_artifacts import diagnostic_path

# Evidence retention is explicitly opted into before tools/models run.
diagnostic_path("")


import json
import re
import subprocess
from pathlib import Path

SRC = Path(str(diagnostic_path("booktext-hybrid-v12.json")))
SPELL = Path.home() / "AITools/booktext/bin/booktext-spellcheck"
OUT = Path(str(diagnostic_path("booktext-hybrid-v14-consensus-audit.json")))

data = json.loads(SRC.read_text(encoding="utf-8"))
records = data["resolved_units"]

tokens = []

for ri, r in enumerate(records):
    for token in r.get("final_tokens", []):
        tokens.append({
            "record_index": ri,
            "unit": r["unit"],
            "status": r["status"],
            "token": token,
            "vision": r.get("vision"),
            "tesseract": r.get("tesseract"),
        })


def lexical(token):
    # Keep Russian lexical content and internal hyphens.
    x = token.strip()

    x = re.sub(
        r"^[^0-9A-Za-zА-Яа-яЁё]+",
        "",
        x
    )

    x = re.sub(
        r"[^0-9A-Za-zА-Яа-яЁё-]+$",
        "",
        x
    )

    return x


candidates = []

for i, item in enumerate(tokens):
    word = lexical(item["token"])

    if not word:
        continue

    # Pure numbers need no spelling check.
    if re.fullmatch(r"\d+", word):
        continue

    # Only audit tokens containing Cyrillic.
    if not re.search(r"[А-Яа-яЁё]", word):
        continue

    candidates.append((i, word))


# Batch spellcheck.
args = [str(SPELL)] + [word for _, word in candidates]

p = subprocess.run(
    args,
    capture_output=True,
    text=True,
    check=True,
)

results = [
    x.strip()
    for x in p.stdout.splitlines()
    if x.strip()
]

if len(results) != len(candidates):
    raise RuntimeError(
        f"Spellchecker returned {len(results)} results "
        f"for {len(candidates)} tokens"
    )


invalid = []

for (token_index, word), result in zip(candidates, results):

    if result != "INVALID":
        continue

    item = tokens[token_index]

    lo = max(0, token_index - 5)
    hi = min(len(tokens), token_index + 6)

    context_parts = []

    for j in range(lo, hi):
        t = tokens[j]["token"]

        if j == token_index:
            t = f"<<<{t}>>>"

        context_parts.append(t)

    invalid.append({
        "token_index": token_index + 1,
        "unit": item["unit"],
        "status": item["status"],
        "token": item["token"],
        "lexical": word,
        "vision": item["vision"],
        "tesseract": item["tesseract"],
        "context": " ".join(context_parts),
    })


# Particularly important: tokens that were accepted because
# the two OCR engines AGREED.
consensus_invalid = [
    x for x in invalid
    if x["status"] == "AGREE"
]


OUT.write_text(
    json.dumps(
        {
            "engine": "booktext-consensus-audit-v14",
            "total_final_tokens": len(tokens),
            "checked_tokens": len(candidates),
            "invalid_tokens": invalid,
            "consensus_invalid": consensus_invalid,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)


print("===== BOOKTEXT CONSENSUS AUDIT V14 =====")
print("Final tokens:      ", len(tokens))
print("Spellchecked:      ", len(candidates))
print("All INVALID:       ", len(invalid))
print("AGREE but INVALID: ", len(consensus_invalid))

print()
print("===== AGREE BUT INVALID =====")

for x in consensus_invalid:
    print()
    print(
        f"UNIT {x['unit']:03d} | "
        f"TOKEN {x['token_index']:03d}"
    )
    print("WORD:   ", x["token"])
    print("VISION: ", x["vision"])
    print("TESS:   ", x["tesseract"])
    print("CONTEXT:", x["context"])

print()
print("===== OTHER INVALID FINAL TOKENS =====")

for x in invalid:
    if x["status"] == "AGREE":
        continue

    print()
    print(
        f"UNIT {x['unit']:03d} | "
        f"{x['status']}"
    )
    print("WORD:   ", x["token"])
    print("VISION: ", x["vision"])
    print("TESS:   ", x["tesseract"])
    print("CONTEXT:", x["context"])

print()
print("===== OUTPUT =====")
print(OUT)
