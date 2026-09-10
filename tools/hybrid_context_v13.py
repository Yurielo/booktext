#!/usr/bin/env python3

import json
import urllib.request
from pathlib import Path

SRC = Path("/tmp/booktext-hybrid-v12.json")
OUT = Path("/tmp/booktext-hybrid-v13-context.json")

OLLAMA = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen3:8b"

data = json.loads(SRC.read_text(encoding="utf-8"))

results = []

for r in data["resolved_units"]:

    if r["status"] != "REVIEW":
        continue

    context = r.get("context", "")
    vision = r.get("vision", "")
    tess = r.get("tesseract", "")

    prompt = f"""
Ты — третий независимый корректор OCR русского печатного текста.

Нужно восстановить РОВНО тот фрагмент, который стоял на месте <<<...>>>.

Контекст:
{context}

Apple Vision:
{vision}

Tesseract:
{tess}

Правила:
1. Используй прежде всего грамматику, смысл и устойчивые выражения контекста.
2. Можно выбрать Vision.
3. Можно выбрать Tesseract.
4. Если оба OCR ошиблись, можно предложить третий вариант.
5. Нельзя переписывать соседние слова.
6. Сохрани необходимую пунктуацию.
7. Не объясняй в поле "answer" ничего кроме восстановленного фрагмента.
8. confidence от 0 до 1.
9. Если уверенности недостаточно, decision должен быть "uncertain".

Верни только JSON:
{{
  "answer": "...",
  "decision": "vision|tesseract|third|uncertain",
  "confidence": 0.0,
  "reason": "краткая причина"
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

        raw = outer.get("response", "")
        answer = json.loads(raw)

    except Exception as e:
        answer = {
            "answer": "",
            "decision": "error",
            "confidence": 0,
            "reason": str(e),
        }

    result = {
        "unit": r["unit"],
        "context": context,
        "vision": vision,
        "tesseract": tess,
        "vision_confidence": r.get("vision_confidence"),
        "tesseract_confidence": r.get("tesseract_confidence"),
        "gemma_signal": r.get("gemma_signal"),
        "qwen": answer,
    }

    results.append(result)


OUT.write_text(
    json.dumps(
        {
            "engine": "booktext-context-v13",
            "model": MODEL,
            "results": results,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)


print("===== BOOKTEXT CONTEXT V13 =====")

for r in results:

    q = r["qwen"]

    print()
    print("=" * 68)
    print(f"UNIT {r['unit']:03d}")
    print("CONTEXT:", r["context"])
    print("VISION: ", r["vision"])
    print("TESS:   ", r["tesseract"])
    print("GEMMA:  ", r["gemma_signal"])
    print("QWEN:   ", q.get("answer"))
    print("DECISION:", q.get("decision"))
    print("CONF:    ", q.get("confidence"))
    print("REASON:  ", q.get("reason"))

print()
print("===== OUTPUT =====")
print(OUT)
