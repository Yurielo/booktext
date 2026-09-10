#!/usr/bin/env python3

import json
import urllib.request
from pathlib import Path

SRC = Path("/tmp/booktext-hybrid-v12.txt")
OUT = Path("/tmp/booktext-hybrid-v15-semantic-audit.json")

MODEL = "qwen3:8b"
OLLAMA = "http://127.0.0.1:11434/api/generate"

text = SRC.read_text(encoding="utf-8")

prompt = f"""
Ты выполняешь ТОЛЬКО аудит OCR русского печатного текста.

Перед тобой результат двух независимых OCR-систем.
Большинство текста уже правильно.

Найди только места, которые выглядят именно как ошибки распознавания:
- одна буква заменена похожей;
- пропущена буква;
- лишняя буква;
- OCR получил существующее русское слово, но оно явно не подходит по смыслу;
- испорчен перенос слова;
- испорчена цифра, буква или знак.

НЕ исправляй:
- нормальные авторские формулировки;
- специализированные химические/медицинские термины только потому, что они редкие;
- пунктуацию и стиль без явной OCR-ошибки;
- текст, если не уверен.

Особенно внимательно проверяй локальный контекст каждого подозрительного слова.

ТЕКСТ:
{text}

Верни ТОЛЬКО JSON следующего вида:

{{
  "suspects": [
    {{
      "original": "точно как в тексте",
      "suggested": "вероятное чтение",
      "context": "несколько слов до и после",
      "confidence": 0.0,
      "reason": "коротко, почему это похоже именно на OCR-ошибку"
    }}
  ]
}}

Если подозрительных мест нет, верни:
{{"suspects":[]}}
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

with urllib.request.urlopen(req, timeout=300) as resp:
    outer = json.loads(resp.read().decode("utf-8"))

raw = outer.get("response", "")
result = json.loads(raw)

OUT.write_text(
    json.dumps(
        {
            "engine": "booktext-semantic-audit-v15",
            "model": MODEL,
            "suspects": result.get("suspects", [])
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)

print("===== BOOKTEXT SEMANTIC AUDIT V15 =====")

suspects = result.get("suspects", [])

print("Suspects:", len(suspects))

for i, s in enumerate(suspects, 1):
    print()
    print("=" * 68)
    print(f"SUSPECT {i:02d}")
    print("ORIGINAL: ", s.get("original"))
    print("SUGGESTED:", s.get("suggested"))
    print("CONF:     ", s.get("confidence"))
    print("CONTEXT:  ", s.get("context"))
    print("REASON:   ", s.get("reason"))

print()
print("===== OUTPUT =====")
print(OUT)
