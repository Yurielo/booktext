#!/usr/bin/env python3
try:
    from .diagnostic_artifacts import diagnostic_path
except ImportError:
    from diagnostic_artifacts import diagnostic_path

# Evidence retention is explicitly opted into before tools/models run.
diagnostic_path("")


import base64
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "gemma3:4b"


def image_size(path):
    p = subprocess.run(
        ["magick", "identify", "-format", "%w %h", path],
        capture_output=True,
        text=True,
        check=True,
    )
    w, h = map(int, p.stdout.strip().split())
    return w, h


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def vision_bbox_pixels(line, image_w, image_h):
    b = line["bbox"]

    x = float(b.get("x", 0))
    y = float(b.get("y", 0))
    w = float(b.get("width", 0))
    h = float(b.get("height", 0))

    left = int(x * image_w)
    right = int((x + w) * image_w)

    # Vision origin = lower-left.
    top = int((1.0 - (y + h)) * image_h)
    bottom = int((1.0 - y) * image_h)

    return left, top, right, bottom


def make_crop(
    image,
    vision_lines,
    line_numbers,
    destination,
    image_w,
    image_h,
):
    selected = []

    for n in line_numbers:
        index = int(n) - 1

        if 0 <= index < len(vision_lines):
            selected.append(
                vision_bbox_pixels(
                    vision_lines[index],
                    image_w,
                    image_h,
                )
            )

    if not selected:
        raise RuntimeError(
            f"No Vision bbox for lines {line_numbers}"
        )

    left = min(b[0] for b in selected)
    top = min(b[1] for b in selected)
    right = max(b[2] for b in selected)
    bottom = max(b[3] for b in selected)

    # Give the visual OCR some surrounding pixels.
    pad_x = max(12, int(image_w * 0.015))
    pad_y = max(8, int(image_h * 0.008))

    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(image_w, right + pad_x)
    bottom = min(image_h, bottom + pad_y)

    width = max(1, right - left)
    height = max(1, bottom - top)

    subprocess.run(
        [
            "magick",
            image,
            "-crop",
            f"{width}x{height}+{left}+{top}",
            "+repage",
            "-resize",
            "250%",
            "-colorspace",
            "Gray",
            "-contrast-stretch",
            "0.3%x0.3%",
            destination,
        ],
        check=True,
    )

    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
    }


def ollama_transcribe(crop, vision_text, tess_text):
    image_data = base64.b64encode(
        Path(crop).read_bytes()
    ).decode("ascii")

    prompt = f"""
Ты выполняешь OCR, а НЕ литературное редактирование.

Перед тобой маленький фрагмент страницы книги.
Нужно прочитать ТОЧНО те символы, которые видны на изображении.

Два OCR дали варианты:

APPLE VISION:
{vision_text}

TESSERACT:
{tess_text}

Правила:
1. Главный источник — само изображение.
2. Не исправляй авторский стиль.
3. Не модернизируй орфографию.
4. Не пересказывай текст.
5. Не добавляй слов, которых не видно.
6. Сохрани регистр, цифры и знаки препинания.
7. Сохрани дефис в конце строки, если он реально виден.
8. Если один OCR ошибся в букве, выбери то, что реально напечатано.
9. Если правильная фраза собирается из частей двух вариантов,
   собери её по изображению.
10. Верни только JSON:
{{"text":"точная расшифровка"}}
""".strip()

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "images": [image_data],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0,
        },
    }

    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(
        req,
        timeout=600,
    ) as response:
        result = json.loads(
            response.read().decode("utf-8")
        )

    raw = result.get("response", "").strip()

    try:
        decoded = json.loads(raw)
        text = str(decoded.get("text", "")).strip()
    except Exception:
        text = raw.strip()

    return text


def main():
    if len(sys.argv) != 6:
        print(
            "Usage: hybrid_adjudicate_v4.py "
            "<image> <vision.json> <v3.json> "
            "<output.txt> <output.json>"
        )
        raise SystemExit(2)

    image = sys.argv[1]
    vision_path = sys.argv[2]
    audit_path = sys.argv[3]
    output_text = sys.argv[4]
    output_json = sys.argv[5]

    vision = load_json(vision_path)
    audit = load_json(audit_path)

    image_w, image_h = image_size(image)

    vision_lines = vision.get("lines", [])
    units = audit.get("units", [])

    crop_dir = Path(
        str(diagnostic_path("booktext-hybrid-v4-crops"))
    )
    crop_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    final_units = []

    print("===== HYBRID V4 — IMAGE ADJUDICATION =====")
    print("Model:", MODEL)
    print("Image:", f"{image_w}x{image_h}")
    print()

    adjudicated = 0

    for unit in units:
        status = unit.get("status", "")
        chosen = unit.get("text", "")
        source = unit.get("winner", "vision")

        record = dict(unit)

        if status.startswith("REVIEW"):
            adjudicated += 1

            number = unit["unit"]
            crop = crop_dir / f"unit-{number:03d}.png"

            try:
                bbox = make_crop(
                    image,
                    vision_lines,
                    unit.get("vision_lines", []),
                    str(crop),
                    image_w,
                    image_h,
                )

                gemma = ollama_transcribe(
                    str(crop),
                    unit.get("vision", ""),
                    unit.get("tesseract", ""),
                )

                if gemma:
                    chosen = gemma
                    source = "gemma-image"
                else:
                    source = "vision-fallback"

                record["crop"] = str(crop)
                record["crop_bbox"] = bbox
                record["gemma"] = gemma

            except Exception as exc:
                record["gemma_error"] = str(exc)
                source = "vision-fallback"

            print(
                f"===== UNIT {number:02d} ====="
            )
            print(
                "VISION:   ",
                unit.get("vision", ""),
            )
            print(
                "TESS:     ",
                unit.get("tesseract", ""),
            )

            if record.get("gemma"):
                print(
                    "GEMMA:    ",
                    record["gemma"],
                )
            else:
                print(
                    "GEMMA:     ERROR/FALLBACK"
                )
                if record.get("gemma_error"):
                    print(
                        "ERROR:    ",
                        record["gemma_error"],
                    )

            print(
                "FINAL:    ",
                chosen,
            )
            print()

        record["final_text"] = chosen
        record["final_source"] = source

        final_units.append(record)

    with open(
        output_text,
        "w",
        encoding="utf-8",
    ) as f:
        for unit in final_units:
            text = unit.get("final_text", "")

            if text:
                f.write(text)
                f.write("\n")

    with open(
        output_json,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            {
                "engine": "booktext-hybrid-v4",
                "model": MODEL,
                "adjudicated_units": adjudicated,
                "units": final_units,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print("===== SUMMARY =====")
    print(
        "Units:",
        len(final_units),
    )
    print(
        "Image-adjudicated:",
        adjudicated,
    )
    print(
        "Text:",
        output_text,
    )
    print(
        "Audit:",
        output_json,
    )
    print(
        "Crops:",
        crop_dir,
    )


if __name__ == "__main__":
    main()
