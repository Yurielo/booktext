#!/usr/bin/env python3

import base64
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "gemma3:4b"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dimensions(path):
    p = subprocess.run(
        ["magick", "identify", "-format", "%w %h", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return map(int, p.stdout.strip().split())


def bbox_px(line, W, H):
    b = line["bbox"]

    x = float(b["x"])
    y = float(b["y"])
    w = float(b["width"])
    h = float(b["height"])

    left = int(x * W)
    right = int((x + w) * W)

    top = int((1 - (y + h)) * H)
    bottom = int((1 - y) * H)

    return left, top, right, bottom


def crop_context(image, lines, target_numbers, output, W, H):
    wanted = set()

    for n in target_numbers:
        for k in (n - 1, n, n + 1):
            if 1 <= k <= len(lines):
                wanted.add(k)

    boxes = [
        bbox_px(lines[n - 1], W, H)
        for n in sorted(wanted)
    ]

    left = max(0, min(b[0] for b in boxes) - 20)
    right = min(W, max(b[2] for b in boxes) + 20)
    top = max(0, min(b[1] for b in boxes) - 12)
    bottom = min(H, max(b[3] for b in boxes) + 12)

    width = right - left
    height = bottom - top

    subprocess.run(
        [
            "magick",
            image,
            "-crop",
            f"{width}x{height}+{left}+{top}",
            "+repage",
            "-resize",
            "400%",
            "-colorspace",
            "Gray",
            "-sharpen",
            "0x1",
            "-contrast-stretch",
            "0.15%x0.15%",
            output,
        ],
        check=True,
    )


def ask(image, previous, target_v, target_t, following):
    encoded = base64.b64encode(
        Path(image).read_bytes()
    ).decode("ascii")

    prompt = f"""
Ты выполняешь буквальный OCR старой печатной книги.

На изображении видны примерно три строки:
ПРЕДЫДУЩАЯ:
{previous}

СПОРНАЯ — Apple Vision:
{target_v}

СПОРНАЯ — Tesseract:
{target_t}

СЛЕДУЮЩАЯ:
{following}

Твоя задача — прочитать ТОЛЬКО СПОРНУЮ строку непосредственно
с изображения.

КРИТИЧЕСКИЕ ПРАВИЛА:
- изображение важнее вариантов OCR;
- не исправляй текст по смыслу;
- не заменяй слово на более логичное, если буквы этого не подтверждают;
- обязательно сохраняй конечный дефис, если он напечатан;
- сохраняй пунктуацию;
- учитывай соседние строки только для понимания переноса слова;
- не соединяй спорную строку со следующей;
- не добавляй предыдущую или следующую строку;
- верни JSON строго вида:
{{"text":"..."}}.
""".strip()

    data = {
        "model": MODEL,
        "prompt": prompt,
        "images": [encoded],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(
        request,
        timeout=600,
    ) as response:
        obj = json.loads(response.read())

    raw = obj.get("response", "").strip()

    try:
        return json.loads(raw)["text"].strip()
    except Exception:
        return raw


def main():
    if len(sys.argv) != 4:
        print(
            "Usage: hybrid_context_v5.py "
            "<page> <vision.json> <v4.json>"
        )
        raise SystemExit(2)

    page, vision_path, v4_path = sys.argv[1:]

    vision = load(vision_path)
    v4 = load(v4_path)

    lines = vision["lines"]
    units = v4["units"]

    W, H = dimensions(page)

    outdir = Path("/tmp/booktext-v5-context")
    outdir.mkdir(parents=True, exist_ok=True)

    results = []

    # Review every unit where the two OCR engines originally disagreed.
    targets = [
        u for u in units
        if u.get("vision")
        and u.get("tesseract")
        and u.get("vision") != u.get("tesseract")
    ]

    print("===== HYBRID V5 CONTEXT OCR =====")
    print("Targets:", len(targets))
    print()

    for u in targets:
        nums = u.get("vision_lines", [])

        if not nums:
            continue

        target = nums[0]

        previous = (
            lines[target - 2]["text"]
            if target > 1 else ""
        )

        following_index = nums[-1]

        following = (
            lines[following_index]["text"]
            if following_index < len(lines)
            else ""
        )

        crop = outdir / f"unit-{u['unit']:03d}.png"

        crop_context(
            page,
            lines,
            nums,
            str(crop),
            W,
            H,
        )

        try:
            text = ask(
                str(crop),
                previous,
                u.get("vision", ""),
                u.get("tesseract", ""),
                following,
            )
            error = None
        except Exception as exc:
            text = ""
            error = str(exc)

        result = {
            "unit": u["unit"],
            "vision_lines": nums,
            "vision": u.get("vision", ""),
            "tesseract": u.get("tesseract", ""),
            "v4": u.get("final_text", ""),
            "v5": text,
            "crop": str(crop),
            "error": error,
        }

        results.append(result)

        print(f"===== UNIT {u['unit']:02d} =====")
        print("VISION:", u.get("vision", ""))
        print("TESS:  ", u.get("tesseract", ""))
        print("V4:    ", u.get("final_text", ""))
        print("V5:    ", text or "ERROR")
        if error:
            print("ERROR: ", error)
        print()

    path = "/tmp/booktext-hybrid-v5-review.json"

    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            results,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print("===== SAVED =====")
    print(path)
    print(outdir)


if __name__ == "__main__":
    main()
