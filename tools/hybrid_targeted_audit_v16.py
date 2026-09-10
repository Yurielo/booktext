#!/usr/bin/env python3

import json
import urllib.request
from pathlib import Path

SRC = Path("/tmp/booktext-hybrid-v14-consensus-audit.json")
OUT = Path("/tmp/booktext-hybrid-v16-targeted-audit.json")

OLLAMA = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen3:8b"

data = json.loads(SRC.read_text(encoding="utf-8"))

# Берём все INVALID из spellcheck-аудита.
targets = data["invalid_tokens"]

results = []

for x in targets:
    original = x["token"]
    context = x["context"]

    prompt = f"""
Ты проверяешь ОДИН подозрительный OCR-фрагмент русского текста.

КОНТЕКСТ:
{context}

ПОДОЗРИТЕЛЬНЫЙ ФРАГМЕНТ:
{original}

Определи, что здесь происходит.

Возможные классы:
- correct — фрагмент нормальный и его не надо менять;
- hyphen_part — это корректная часть слова, разорванного переносом;
- specialist_term — корректный специальный термин;
- ocr_error — вероятная ошибка OCR;
- uncertain — недостаточно данных.

Если это ocr_error, предложи наиболее вероятное исправление.
Не переписывай соседние слова.
Не исправляй стиль.
Не исправляй редкий термин только потому, что он неизвестен словарю.

Верни ТОЛЬКО JSON:

{{
  "class": "correct|hyphen_part|specialist_term|ocr_error|uncertain",
  "suggested": "...",
  "confidence": 0.0,
  "reason": "коротко"
}}
""".strip()

    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            outer = json.loads(resp.read().decode("utf-8"))

        answer = json.loads(outer.get("response", "{}"))

    except Exception as e:
        answer = {
            "class": "error",
            "suggested": "",
            "confidence": 0,
            "reason": str(e),
        }

    results.append({
        **x,
        "qwen": answer,
    })


OUT.write_text(
    json.dumps(
        {
            "engine": "booktext-targeted-audit-v16",
            "model": MODEL,
            "results": results,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)

print("===== BOOKTEXT TARGETED AUDIT V16 =====")

counts = {}

for r in results:
    cls = r["qwen"].get("class", "unknown")
    counts[cls] = counts.get(cls, 0) + 1

print()
print("===== COUNTS =====")

for k in sorted(counts):
    print(f"{k:18s}: {counts[k]}")

print()
print("===== RESULTS =====")

for r in results:
    q = r["qwen"]

    print()
    print("=" * 68)
    print(f"UNIT {r['unit']:03d}")
    print("ORIGINAL: ", r["token"])
    print("STATUS:   ", r["status"])
    print("CONTEXT:  ", r["context"])
    print("CLASS:    ", q.get("class"))
    print("SUGGESTED:", q.get("suggested"))
    print("CONF:     ", q.get("confidence"))
    print("REASON:   ", q.get("reason"))

print()
print("===== KEY ERROR CHECK =====")

expected = {
    "стралает": "страдает",
    "кукурузс,": "кукурузе,",
    "подогрейтс": "подогрейте",
    "замстили": "заметили",
    "попыгайтесь": "попытайтесь",
}

by_original = {
    r["token"]: r["qwen"]
    for r in results
}

for src, expected_fix in expected.items():
    q = by_original.get(src)

    if not q:
        print("MISS    |", src, "->", expected_fix)
        continue

    got = q.get("suggested", "")
    cls = q.get("class")

    ok = (
        cls == "ocr_error"
        and got.rstrip(".,;:!?") == expected_fix.rstrip(".,;:!?")
    )

    print(
        "PASS" if ok else "FAIL",
        "|",
        src,
        "->",
        got,
        "| class=",
        cls,
    )

print()
print("===== OUTPUT =====")
print(OUT)
