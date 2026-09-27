#!/usr/bin/env python3

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path


OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "gemma3:4b"
DEFAULT_OCR_LANGUAGES = "rus+eng"
DEFAULT_OCR_ENGINE = "surya"
SURYA_CLI = Path.home() / "AITools/booktext/benchmark/surya-env/bin/surya_ocr"
VISION_OCR = Path.home() / "AITools/booktext/bin/visionocr"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
VISUAL_TYPES = {"figure", "photo", "diagram", "chart", "map", "table", "illustration", "handwriting"}
EXCLUDED_OCR_REGION_TYPES = {
    "handwriting", "figure", "photo", "diagram", "chart", "map", "illustration",
}
MAX_AUTOMATIC_HANDWRITING_AREA_PERCENT = 20.0


def run(command, check=True, capture=False, quiet=False):
    command = [str(x) for x in command]
    if not quiet:
        print("→", " ".join(command))
    return subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require(command, optional=False):
    if shutil.which(command) is None:
        if optional:
            return False
        print(f"Ошибка: не найдена программа {command}")
        sys.exit(1)
    return True


def parse_page_spec(spec):
    if not spec:
        return None

    pages = set()
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue

        if "-" in part:
            bits = part.split("-", 1)
            try:
                start = int(bits[0])
                end = int(bits[1])
            except ValueError:
                raise ValueError(f"Некорректный диапазон страниц: {part}")

            if start < 1 or end < 1 or end < start:
                raise ValueError(f"Некорректный диапазон страниц: {part}")

            pages.update(range(start, end + 1))
        else:
            try:
                page = int(part)
            except ValueError:
                raise ValueError(f"Некорректный номер страницы: {part}")

            if page < 1:
                raise ValueError(f"Номер страницы должен быть >= 1: {part}")
            pages.add(page)

    return pages


def get_pdf_page_count(source):
    if shutil.which("pdfinfo"):
        result = run(["pdfinfo", source], check=False, capture=True, quiet=True)
        if result.returncode == 0:
            match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
            if match:
                return int(match.group(1))

    if shutil.which("qpdf"):
        result = run(["qpdf", "--show-npages", source], check=False, capture=True, quiet=True)
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())

    raise RuntimeError("Не удалось определить количество страниц PDF.")


def get_djvu_page_count(source):
    if not shutil.which("djvused"):
        raise RuntimeError("Для подсчёта страниц DjVu требуется djvused (DjVuLibre).")

    result = run(["djvused", source, "-e", "n"], check=False, capture=True, quiet=True)
    if result.returncode == 0:
        value = result.stdout.strip()
        if value.isdigit():
            return int(value)

    raise RuntimeError("Не удалось определить количество страниц DjVu.")


def choose_pages(total_pages, include_spec, exclude_spec):
    try:
        include = parse_page_spec(include_spec)
        exclude = parse_page_spec(exclude_spec) or set()
    except ValueError as error:
        print(f"Ошибка: {error}")
        sys.exit(1)

    if include is None:
        selected = set(range(1, total_pages + 1))
    else:
        selected = set(include)

    invalid = sorted(p for p in selected | exclude if p > total_pages)
    if invalid:
        print(
            f"Ошибка: в документе {total_pages} стр., "
            f"но запрошены отсутствующие страницы: {invalid}"
        )
        sys.exit(1)

    selected -= exclude
    selected = sorted(selected)

    if not selected:
        print("Ошибка: после применения диапазонов не осталось страниц для обработки.")
        sys.exit(1)

    return selected


def preprocess_image(source, destination):
    run([
        "magick",
        source,
        "-auto-orient",
        "-deskew", "40%",
        "-colorspace", "Gray",
        "-contrast-stretch", "0.5%x0.5%",
        destination,
    ])


def render_pdf_page(source, page_number, destination, temp_dir):
    prefix = temp_dir / f"pdftoppm-{page_number:04d}"

    result = run([
        "pdftoppm",
        "-f", str(page_number),
        "-l", str(page_number),
        "-singlefile",
        "-png",
        "-r", "250",
        source,
        prefix,
    ], check=False, capture=True)

    ppm_output = Path(str(prefix) + ".png")
    if result.returncode == 0 and ppm_output.exists() and ppm_output.stat().st_size > 0:
        preprocess_image(ppm_output, destination)
        return

    print(f"  pdftoppm не смог обработать страницу {page_number}; пробую MuPDF.")

    if not shutil.which("mutool"):
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"Не удалось отрисовать страницу {page_number}. "
            f"MuPDF (mutool) не установлен.\n{details}"
        )

    raw_output = temp_dir / f"mutool-{page_number:04d}.png"
    result2 = run([
        "mutool", "draw",
        "-F", "png",
        "-r", "250",
        "-o", raw_output,
        source,
        str(page_number),
    ], check=False, capture=True)

    if result2.returncode != 0 or not raw_output.exists():
        details = (result2.stderr or result2.stdout or "").strip()
        raise RuntimeError(
            f"MuPDF также не смог обработать страницу {page_number}.\n{details}"
        )

    preprocess_image(raw_output, destination)


def render_djvu_page(source, page_number, destination, temp_dir):
    raw = temp_dir / f"djvu-{page_number:04d}.tiff"
    result = run([
        "ddjvu",
        f"-page={page_number}",
        "-format=tiff",
        "-quality=90",
        source,
        raw,
    ], check=False, capture=True)

    if result.returncode != 0 or not raw.exists():
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"Не удалось извлечь страницу {page_number} из DjVu.\n{details}"
        )

    preprocess_image(raw, destination)


def prepare_pages(sources, pages_dir, temp_dir, include_spec, exclude_spec):
    first = sources[0]

    if len(sources) > 1:
        if any(path.suffix.lower() not in IMAGE_EXTENSIONS for path in sources):
            raise RuntimeError(
                "Несколько входных файлов одновременно поддерживаются только для изображений."
            )

        selected = choose_pages(len(sources), include_spec, exclude_spec)
        pages = []

        for position in selected:
            image_source = sources[position - 1]
            destination = pages_dir / f"page-{position:04d}.png"
            print(f"Подготовка изображения {position}: {image_source.name}")
            preprocess_image(image_source, destination)
            pages.append((position, destination))

        return pages, len(sources)

    extension = first.suffix.lower()

    if extension == ".pdf":
        total = get_pdf_page_count(first)
        selected = choose_pages(total, include_spec, exclude_spec)
        pages = []

        for page_number in selected:
            destination = pages_dir / f"page-{page_number:04d}.png"
            print(f"Подготовка PDF-страницы {page_number}/{total}")
            render_pdf_page(first, page_number, destination, temp_dir)
            pages.append((page_number, destination))

        return pages, total

    if extension in {".djvu", ".djv"}:
        total = get_djvu_page_count(first)
        selected = choose_pages(total, include_spec, exclude_spec)
        pages = []

        for page_number in selected:
            destination = pages_dir / f"page-{page_number:04d}.png"
            print(f"Подготовка DjVu-страницы {page_number}/{total}")
            render_djvu_page(first, page_number, destination, temp_dir)
            pages.append((page_number, destination))

        return pages, total

    if extension in IMAGE_EXTENSIONS:
        selected = choose_pages(1, include_spec, exclude_spec)
        if 1 not in selected:
            raise RuntimeError("Для одиночного изображения доступна только страница 1.")

        destination = pages_dir / "page-0001.png"
        preprocess_image(first, destination)
        return [(1, destination)], 1

    raise RuntimeError(f"Формат {extension} пока не поддерживается.")


def ollama_request(model, prompt, image_path, json_mode=False, timeout=600):
    image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")

    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_base64],
        "stream": False,
        "options": {"temperature": 0.1},
    }

    if json_mode:
        payload["format"] = "json"

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result.get("response", "").strip()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Ollama недоступна или вернула ошибку: {error}")


def detect_visual_regions(page, model):
    prompt = r"""
Ты анализируешь страницу отсканированной книги.

Найди только значимые НЕтекстовые или смешанные визуальные области:
- handwriting (рукописные пометки, подчёркивания, маргиналии)
- figure
- illustration
- photo
- diagram
- chart
- map
- table

Не отмечай:
- обычные абзацы текста;
- заголовки;
- номера страниц;
- одиночные декоративные линии;
- маленькие символы без самостоятельного смысла.

Верни СТРОГО JSON-объект такого вида:
{
  "regions": [
    {
      "type": "diagram",
      "bbox": [x0, y0, x1, y1],
      "confidence": 0.95
    }
  ]
}

bbox — координаты в процентах от 0 до 100:
x0 = левая граница,
y0 = верхняя,
x1 = правая,
y1 = нижняя.

Если значимых визуальных областей нет:
{"regions":[]}

Не добавляй никакого текста вне JSON.
""".strip()

    raw = ollama_request(model, prompt, page, json_mode=True)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

    regions = []
    for item in parsed.get("regions", []):
        if not isinstance(item, dict):
            continue

        region_type = str(item.get("type", "figure")).lower().strip()
        if region_type not in VISUAL_TYPES:
            continue

        bbox = item.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue

        try:
            x0, y0, x1, y1 = [float(value) for value in bbox]
        except (TypeError, ValueError):
            continue

        x0 = max(0.0, min(100.0, x0))
        y0 = max(0.0, min(100.0, y0))
        x1 = max(0.0, min(100.0, x1))
        y1 = max(0.0, min(100.0, y1))

        if x1 <= x0 or y1 <= y0:
            continue

        area = (x1 - x0) * (y1 - y0)
        if area < 1.0:
            continue

        # A handwritten annotation can be substantial, but cannot credibly
        # occupy most of a page that also contains printed body text.  This is
        # a fail-safe: a broad false positive would otherwise erase the page.
        if (
            region_type == "handwriting"
            and area > MAX_AUTOMATIC_HANDWRITING_AREA_PERCENT * 100
        ):
            continue

        confidence = item.get("confidence", 0.5)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.5

        regions.append({
            "type": region_type,
            "bbox": [x0, y0, x1, y1],
            "confidence": confidence,
        })

    # Убираем почти одинаковые дубликаты.
    unique = []
    for candidate in sorted(regions, key=lambda r: (r["bbox"][1], r["bbox"][0])):
        duplicate = False
        for existing in unique:
            a = candidate["bbox"]
            b = existing["bbox"]
            if all(abs(a[i] - b[i]) < 3 for i in range(4)):
                duplicate = True
                break
        if not duplicate:
            unique.append(candidate)

    for region in unique:
        region["source"] = "automatic_detection"
        region["ocr_action"] = (
            "exclude" if region["type"] in EXCLUDED_OCR_REGION_TYPES else "include"
        )
    return unique


def load_region_map(path):
    """Load deliberately small, reviewable per-page OCR exclusions."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Не удалось прочитать карту областей: {error}") from error

    if payload.get("schema_version") != 1 or not isinstance(payload.get("pages"), list):
        raise RuntimeError(
            "Карта областей должна содержать schema_version: 1 и массив pages."
        )

    by_page = {}
    for page_item in payload["pages"]:
        if not isinstance(page_item, dict) or not isinstance(page_item.get("page"), int):
            raise RuntimeError("У каждой записи карты должен быть целый номер page.")
        page_number = page_item["page"]
        if page_number < 1 or page_number in by_page:
            raise RuntimeError("Номера страниц в карте должны быть уникальными и >= 1.")

        regions = []
        for item in page_item.get("regions", []):
            if not isinstance(item, dict) or item.get("action") != "exclude":
                raise RuntimeError("Каждая область карты должна иметь action: exclude.")
            region_type = str(item.get("type", "handwriting")).lower().strip()
            if region_type not in EXCLUDED_OCR_REGION_TYPES:
                raise RuntimeError(f"Недопустимый тип исключаемой области: {region_type}")
            bbox = item.get("bbox_percent")
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise RuntimeError("bbox_percent должен содержать четыре координаты.")
            try:
                x0, y0, x1, y1 = [float(value) for value in bbox]
            except (TypeError, ValueError) as error:
                raise RuntimeError("Координаты bbox_percent должны быть числами.") from error
            if not (0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 100):
                raise RuntimeError("bbox_percent должен находиться в диапазоне 0..100.")
            regions.append({
                "type": region_type,
                "bbox": [x0, y0, x1, y1],
                "confidence": None,
                "source": "manual_region_map",
                "ocr_action": "exclude",
            })
        by_page[page_number] = regions
    return by_page


def image_dimensions(page):
    result = run([
        "magick", "identify",
        "-format", "%w %h",
        page,
    ], capture=True, quiet=True)
    width, height = result.stdout.strip().split()
    return int(width), int(height)


def percent_bbox_to_pixels(bbox, width, height, padding=8):
    x0, y0, x1, y1 = bbox

    left = max(0, int(width * x0 / 100) - padding)
    top = max(0, int(height * y0 / 100) - padding)
    right = min(width, int(width * x1 / 100) + padding)
    bottom = min(height, int(height * y1 / 100) + padding)

    return left, top, right, bottom


def mask_and_extract_visuals(page, page_number, regions, images_dir, work_dir):
    if not regions:
        return page, []

    width, height = image_dimensions(page)
    masked = work_dir / f"page-{page_number:04d}-masked.png"

    draw_parts = []
    visual_records = []

    for index, region in enumerate(regions, start=1):
        left, top, right, bottom = percent_bbox_to_pixels(
            region["bbox"], width, height
        )

        crop_width = right - left
        crop_height = bottom - top

        if crop_width < 30 or crop_height < 30:
            continue

        crop_name = f"page-{page_number:04d}-image-{index:02d}.png"
        crop_path = images_dir / crop_name

        run([
            "magick",
            page,
            "-crop", f"{crop_width}x{crop_height}+{left}+{top}",
            "+repage",
            crop_path,
        ], quiet=True)

        if region.get("ocr_action") == "exclude":
            draw_parts.append(f"rectangle {left},{top} {right},{bottom}")

        visual_records.append({
            "index": index,
            "type": region["type"],
            "confidence": region["confidence"],
            "source": region.get("source", "automatic_detection"),
            "ocr_action": region.get("ocr_action", "include"),
            "bbox_percent": region["bbox"],
            "bbox_pixels": [left, top, right, bottom],
            "image": str(crop_path),
        })

    if not visual_records:
        return page, []
    if not draw_parts:
        return page, visual_records

    command = [
        "magick",
        page,
        "-fill", "white",
        "-stroke", "white",
    ]
    for rectangle in draw_parts:
        command.extend(["-draw", rectangle])
    command.append(masked)

    run(command, quiet=True)

    return masked, visual_records


def recognize_page(page, languages):
    """Legacy full-page Tesseract OCR."""
    result = run([
        "tesseract",
        page,
        "stdout",
        "-l", languages,
        "--psm", "3",
        "--dpi", "300",
    ], capture=True)

    return result.stdout.strip()


def require_surya():
    """Check the isolated Surya installation and llama.cpp backend."""
    if not SURYA_CLI.is_file():
        raise RuntimeError(
            "Surya OCR не найден: "
            f"{SURYA_CLI}. "
            "Ожидается установленное окружение booktext/benchmark/surya-env."
        )

    if not shutil.which("llama-server"):
        raise RuntimeError(
            "llama-server не найден в PATH. "
            "Surya на Apple Silicon использует llama.cpp backend."
        )


def _surya_html_to_text(value):
    """
    Convert Surya block HTML to readable plain text without adding
    semantic corrections of our own.
    """
    if not value:
        return ""

    import html
    import re

    text = str(value)

    # Preserve useful structural breaks before stripping tags.  These are raw
    # regular expressions: escaping ``\\s`` or ``\\t`` here would match a
    # literal backslash and a letter, which corrupts ordinary text such as
    # "treatment".
    text = re.sub(
        r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</li\s*>|"
        r"</tr\s*>|</h[1-6]\s*>",
        "\n",
        text,
    )
    text = re.sub(r"(?i)</t[dh]\s*>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)

    text = html.unescape(text)
    text = text.replace("\xa0", " ")

    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)

    return "\n".join(lines)


def _surya_page_text(page_result):
    """
    Extract text from current Surya OCR output.

    Surya OCR 2 returns blocks in reading order. Each block normally
    contains HTML plus its label, bbox/polygon and confidence.
    """
    if not isinstance(page_result, dict):
        return ""

    blocks = page_result.get("blocks")

    if isinstance(blocks, list):
        parts = []

        for block in blocks:
            if not isinstance(block, dict):
                continue

            value = (
                block.get("html")
                or block.get("text")
                or block.get("markdown")
                or ""
            )

            text = _surya_html_to_text(value)

            if text:
                parts.append(text)

        if parts:
            return "\n".join(parts).strip()

    # Compatibility fallback for older Surya output.
    text_lines = page_result.get("text_lines")
    if isinstance(text_lines, list):
        parts = []

        for line in text_lines:
            if isinstance(line, dict):
                value = line.get("text", "")
            else:
                value = str(line)

            value = value.strip()
            if value:
                parts.append(value)

        if parts:
            return "\n".join(parts).strip()

    for key in ("text", "html", "markdown"):
        if page_result.get(key):
            return _surya_html_to_text(page_result[key])

    return ""


def recognize_pages_surya(pages, output_dir, temp_dir):
    """
    Run Surya once for all selected page images.

    Real copies are staged because directory input with symlinked images
    proved unreliable in our benchmark.
    """
    if not pages:
        return {}

    require_surya()

    input_dir = temp_dir / "surya-input"
    input_dir.mkdir(parents=True, exist_ok=True)

    # Never feed stale pages from a previous booktext run.
    for page_number, page in pages:
        destination = input_dir / f"page-{page_number:04d}.png"
        shutil.copy2(page, destination)

    # Keep a previous successful OCR result intact until Surya has produced a
    # complete, readable replacement.  A failed rerun must not erase review
    # evidence from the earlier run.
    staging_dir = output_dir.parent / (
        f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    )
    staging_dir.mkdir(parents=True, exist_ok=False)

    print()
    print("===== Surya OCR =====")
    print(f"Пакетная обработка страниц: {len(pages)}")

    try:
        run([
            str(SURYA_CLI),
            str(input_dir),
            "--output_dir", str(staging_dir),
        ])
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    result_files = list(staging_dir.rglob("results.json"))

    if not result_files:
        raise RuntimeError(
            "Surya завершился, но results.json не найден."
        )

    merged = {}

    for result_file in result_files:
        try:
            payload = json.loads(
                result_file.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Не удалось прочитать Surya output {result_file}: {error}"
            ) from error

        if isinstance(payload, dict):
            merged.update(payload)

    texts = {}
    missing = []

    for page_number, _page in pages:
        key = f"page-{page_number:04d}"
        page_data = merged.get(key)

        if isinstance(page_data, list):
            page_result = page_data[0] if page_data else {}
        elif isinstance(page_data, dict):
            page_result = page_data
        else:
            page_result = {}

        text = _surya_page_text(page_result)

        if not text:
            missing.append(page_number)
        else:
            texts[page_number] = text

    if missing:
        raise RuntimeError(
            "Surya не вернул текст для страниц: "
            + ", ".join(str(x) for x in missing)
        )

    previous_dir = None
    if output_dir.exists():
        previous_dir = output_dir.parent / (
            f".{output_dir.name}.previous-{uuid.uuid4().hex}"
        )
        os.replace(output_dir, previous_dir)

    try:
        os.replace(staging_dir, output_dir)
    except OSError:
        if previous_dir is not None and previous_dir.exists():
            os.replace(previous_dir, output_dir)
        raise
    else:
        if previous_dir is not None:
            shutil.rmtree(previous_dir)

    print(f"Surya OCR получен для страниц: {len(texts)}")
    return texts


def describe_visual(image, model, visual_type):
    prompt = f"""
Это фрагмент страницы книги. Тип области предварительно определён как: {visual_type}.

Опиши изображение по-русски, без лишней воды.

Если это схема или диаграмма:
- объясни структуру;
- перечисли узлы, стрелки, связи;
- передай все читаемые подписи;
- объясни, что показывает схема.

Если это таблица:
- передай заголовки;
- опиши строки и столбцы;
- по возможности воспроизведи содержимое структурированно.

Если это фотография или рисунок:
- опиши существенные объекты и подписи.

Не выдумывай неразборчивый текст. Если надпись не читается — так и укажи.
""".strip()

    try:
        return ollama_request(model, prompt, image, json_mode=False)
    except RuntimeError as error:
        return f"Описание не создано: {error}"


def require_vision():
    if not VISION_OCR.is_file():
        raise RuntimeError(
            "Apple Vision OCR helper не найден: "
            f"{VISION_OCR}"
        )


def _qa_tokenize(text):
    return re.findall(
        r"[+-]?(?:\d+(?:[.,]\d+)?|[¼½¾])|"
        r"[A-Za-zА-Яа-яЁё0-9]+(?:[-–—][A-Za-zА-Яа-яЁё0-9]+)*",
        text or "",
        flags=re.UNICODE,
    )


def _qa_norm(text):
    return (
        str(text)
        .lower()
        .replace("ё", "е")
        .replace("–", "-")
        .replace("—", "-")
    )


def _qa_collapsed(tokens):
    value = "".join(_qa_norm(x) for x in tokens)
    return re.sub(r"[^a-zа-я0-9¼½¾]+", "", value, flags=re.IGNORECASE)


def _qa_is_word_segmentation(tokens):
    """Return true only for a harmless split inside alphabetic text."""
    return bool(tokens) and all(
        re.fullmatch(r"[A-Za-zА-Яа-яЁё]+", token) is not None
        for token in tokens
    )


def _vision_union_bbox(items):
    boxes = []

    for item in items:
        bbox = item.get("bbox")
        if not isinstance(bbox, dict):
            continue

        try:
            x = float(bbox["x"])
            y = float(bbox["y"])
            width = float(bbox["width"])
            height = float(bbox["height"])
        except (KeyError, TypeError, ValueError):
            continue

        boxes.append([
            x,
            y,
            x + width,
            y + height,
        ])

    if not boxes:
        return None

    return [
        min(x[0] for x in boxes),
        min(x[1] for x in boxes),
        max(x[2] for x in boxes),
        max(x[3] for x in boxes),
    ]


def vision_qa_page(page, page_number, surya_text, vision_dir):
    """
    Compare Surya text with independent Apple Vision OCR.

    This function NEVER modifies Surya text. It only returns a review queue.
    """
    from difflib import SequenceMatcher

    require_vision()

    vision_dir.mkdir(parents=True, exist_ok=True)
    vision_json = vision_dir / f"page-{page_number:04d}.json"

    process = subprocess.run(
        [str(VISION_OCR), str(page)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if process.returncode != 0:
        error = process.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Vision OCR завершился с кодом {process.returncode}: {error}"
        )

    raw = process.stdout.decode("utf-8", errors="replace")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Vision OCR вернул некорректный JSON: {error}"
        ) from error

    vision_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    surya_tokens = _qa_tokenize(surya_text)

    vision_items = []

    for word in payload.get("words", []):
        if not isinstance(word, dict):
            continue

        text = str(word.get("text", "")).strip()
        tokens = _qa_tokenize(text)

        for token in tokens:
            vision_items.append({
                "text": token,
                "confidence": float(word.get("confidence", 0) or 0),
                "bbox": word.get("bbox"),
            })

    vision_tokens = [item["text"] for item in vision_items]

    matcher = SequenceMatcher(
        None,
        [_qa_norm(x) for x in surya_tokens],
        [_qa_norm(x) for x in vision_tokens],
        autojunk=False,
    )

    disagreements = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        surya_part = surya_tokens[i1:i2]
        vision_part_items = vision_items[j1:j2]
        vision_part = [x["text"] for x in vision_part_items]

        # Ignore harmless segmentation differences:
        # "иммунитет" vs "имму нитет", etc.
        if (
            surya_part
            and vision_part
            and _qa_is_word_segmentation(surya_part)
            and _qa_is_word_segmentation(vision_part)
            and _qa_collapsed(surya_part) == _qa_collapsed(vision_part)
        ):
            continue

        confidence = None
        if vision_part_items:
            confidence = sum(
                item["confidence"] for item in vision_part_items
            ) / len(vision_part_items)

        disagreement = {
            "page": page_number,
            "kind": tag,
            "surya": " ".join(surya_part),
            "vision": " ".join(vision_part),
            "vision_confidence": confidence,
            "vision_bbox": _vision_union_bbox(vision_part_items),
        }

        # Very large mismatches matter mainly as completeness warnings.
        if len(surya_part) > 8 or len(vision_part) > 8:
            disagreement["category"] = "completeness"
        else:
            disagreement["category"] = "text"

        disagreements.append(disagreement)

    return {
        "page": page_number,
        "agreement": matcher.ratio(),
        "surya_words": len(surya_tokens),
        "vision_words": len(vision_tokens),
        "vision_json": str(vision_json),
        "differences": disagreements,
    }


def create_searchable_pdf(page_records, output_pdf, temp_dir, languages):
    if not page_records:
        return False

    if any(
        visual.get("ocr_action") == "exclude"
        for record in page_records
        for visual in record.get("visuals", [])
    ):
        print(
            "Поисковый PDF не создан: есть исключённые области. "
            "Нельзя безопасно делать независимый OCR-слой, возвращающий "
            "рукопись или текст внутри иллюстраций."
        )
        return False

    source_images = [record["page_image_absolute"] for record in page_records]
    intermediate_pdf = temp_dir / "selected_pages.pdf"

    try:
        run([
            "magick",
            *source_images,
            intermediate_pdf,
        ])

        run([
            "ocrmypdf",
            "--force-ocr",
            "--deskew",
            "--rotate-pages",
            "-l", languages,
            intermediate_pdf,
            output_pdf,
        ])

        return True

    except subprocess.CalledProcessError:
        print(
            "Предупреждение: поисковый PDF создать не удалось. "
            "Текст, Markdown и изображения всё равно сохранены."
        )
        return False


def write_review_outputs(review_json_path, review_text_path, qa_pages, review_items):
    """Persist QA evidence without treating failed pages as clean pages."""
    failed_pages = [
        item for item in qa_pages
        if isinstance(item, dict) and item.get("error")
    ]
    status = "complete" if not failed_pages else "partial"

    review_json_path.write_text(
        json.dumps(
            {
                "policy": (
                    "Surya remains the primary OCR. Apple Vision differences "
                    "are review candidates only; no automatic text replacement "
                    "is performed."
                ),
                "status": status,
                "pages": qa_pages,
                "review": review_items,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    review_lines = [
        "BOOKTEXT OCR REVIEW",
        "",
        "Основной текст: Surya.",
        "Apple Vision используется только для независимой проверки.",
        "Ни одно расхождение ниже автоматически не исправляло book_clean.txt.",
        "",
    ]

    if failed_pages:
        review_lines.extend([
            "СТАТУС: НЕПОЛНАЯ ПРОВЕРКА.",
            "Apple Vision не завершил проверку следующих страниц:",
        ])
        for item in failed_pages:
            review_lines.append(
                f"- страница {item.get('page', '—')}: {item['error']}"
            )
        review_lines.append("")

    current_page = None
    for item in review_items:
        page_number = item["page"]

        if page_number != current_page:
            current_page = page_number
            review_lines.extend([
                "",
                f"===== СТРАНИЦА {page_number} =====",
                "",
            ])

        confidence = item.get("vision_confidence")
        confidence_text = (
            f"{confidence:.3f}"
            if isinstance(confidence, (int, float))
            else "—"
        )

        review_lines.extend([
            f"[{item.get('category', 'text')}] "
            f"{item.get('kind', '').upper()}",
            f"SURYA : {item.get('surya', '')}",
            f"VISION: {item.get('vision', '')}",
            f"VISION CONFIDENCE: {confidence_text}",
            "",
        ])

    if not review_items and not failed_pages:
        review_lines.append(
            "Значимых расхождений Surya / Apple Vision не найдено."
        )

    review_text_path.write_text(
        "\n".join(review_lines).rstrip() + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Локальная оцифровка PDF, DjVu и изображений: OCR, "
            "выбор страниц, обнаружение рисунков и Markdown."
        )
    )

    parser.add_argument(
        "sources",
        nargs="+",
        help="Один PDF/DjVu/файл изображения или несколько изображений"
    )

    parser.add_argument(
        "--lang",
        default=DEFAULT_OCR_LANGUAGES,
        help=(
            "Языки Tesseract для режима --ocr tesseract и поискового PDF: "
            "eng, rus или rus+eng (по умолчанию rus+eng)"
        )
    )

    parser.add_argument(
        "--ocr",
        choices=("surya", "tesseract"),
        default=DEFAULT_OCR_ENGINE,
        help=(
            "Движок основного OCR: surya (по умолчанию) "
            "или tesseract (старый режим)"
        )
    )

    parser.add_argument(
        "--pages",
        help='Обрабатываемые страницы, например: "1-20,30-36,50"'
    )

    parser.add_argument(
        "--exclude-pages",
        help='Исключаемые страницы, например: "78-83,96"'
    )

    parser.add_argument(
        "--region-map",
        type=Path,
        help=(
            "JSON-карта ручных исключений OCR по областям страницы. "
            "Для указанной страницы она заменяет автоматическое обнаружение."
        ),
    )

    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Визуальная модель Ollama (по умолчанию {DEFAULT_MODEL})"
    )

    parser.add_argument(
        "--raw-ocr",
        action="store_true",
        help=(
            "Не искать и не описывать визуальные области; "
            "выполнять только OCR страницы"
        )
    )

    parser.add_argument(
        "--no-descriptions",
        action="store_true",
        help="Не создавать подробные описания найденных изображений"
    )

    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Не создавать поисковый PDF"
    )

    parser.add_argument(
        "--no-qa",
        action="store_true",
        help=(
            "Не запускать независимую проверку Surya через Apple Vision "
            "и не создавать очередь review"
        )
    )

    args = parser.parse_args()

    sources = [Path(x).expanduser().resolve() for x in args.sources]

    for source in sources:
        if not source.exists():
            print(f"Ошибка: файл не найден: {source}")
            sys.exit(1)

    if len(sources) > 1 and any(
        source.suffix.lower() not in IMAGE_EXTENSIONS for source in sources
    ):
        print(
            "Ошибка: несколько файлов одновременно можно передавать "
            "только если все они являются изображениями."
        )
        sys.exit(1)

    source = sources[0]

    manual_regions = {}
    if args.region_map:
        try:
            manual_regions = load_region_map(args.region_map.expanduser().resolve())
        except RuntimeError as error:
            print(f"Ошибка: {error}")
            sys.exit(1)

    if args.ocr == "surya":
        try:
            require_surya()

            if not args.no_qa:
                require_vision()

        except RuntimeError as error:
            print(f"Ошибка: {error}")
            sys.exit(1)

    # Tesseract is still required by the legacy OCR mode and OCRmyPDF.
    if args.ocr == "tesseract" or not args.no_pdf:
        require("tesseract")

    require("magick")

    if not args.no_pdf:
        require("ocrmypdf")

    require("pdftoppm")
    require("ddjvu")
    require("djvused")

    if source.suffix.lower() == ".pdf":
        require("mutool", optional=True)
        require("qpdf", optional=True)

    output_dir = source.parent / f"{source.stem}_book"
    pages_dir = output_dir / "pages"
    images_dir = output_dir / "images"
    descriptions_dir = output_dir / "descriptions"
    surya_dir = output_dir / "surya"
    vision_dir = output_dir / "vision"
    work_dir = output_dir / ".work"

    output_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)
    descriptions_dir.mkdir(exist_ok=True)
    work_dir.mkdir(exist_ok=True)

    markdown_path = output_dir / "book.md"
    text_path = output_dir / "book_clean.txt"
    json_path = output_dir / "book.json"
    review_json_path = output_dir / "review.json"
    review_text_path = output_dir / "review.txt"
    searchable_pdf = output_dir / "book_ocr.pdf"

    print()
    if len(sources) == 1:
        print("Исходный файл:", source)
    else:
        print(f"Исходных изображений: {len(sources)}")
    print("Папка результата:", output_dir)
    print("OCR engine:", args.ocr)
    print("Язык OCR/PDF:", args.lang)
    print(
        "Режим:",
        "сырой OCR"
        if args.raw_ocr
        else "OCR + обнаружение визуальных областей"
    )
    print()

    records = []
    qa_pages = []
    review_items = []

    with tempfile.TemporaryDirectory(prefix="booktext_") as temp:
        temp_dir = Path(temp)

        try:
            pages, total_pages = prepare_pages(
                sources,
                pages_dir,
                temp_dir,
                args.pages,
                args.exclude_pages,
            )
        except RuntimeError as error:
            print(f"Ошибка: {error}")
            sys.exit(1)

        print()
        print(f"Будет обработано страниц: {len(pages)} из {total_pages}")
        print()

        for mapped_page in manual_regions:
            if mapped_page > total_pages:
                print(
                    f"Ошибка: карта областей содержит отсутствующую страницу {mapped_page}."
                )
                sys.exit(1)

        page_work = {}
        for page_number, page in pages:
            regions = []
            visual_records = []
            ocr_page = page
            try:
                # A map entry (including an empty one) is a reviewed decision
                # and therefore takes precedence over heuristic detection.
                if page_number in manual_regions:
                    regions = manual_regions[page_number]
                elif not args.raw_ocr:
                    regions = detect_visual_regions(page, args.model)

                if regions:
                    print(
                        f"Страница {page_number}: областей для обработки/исключения: "
                        f"{len(regions)}"
                    )
                elif not args.raw_ocr:
                    print(f"Страница {page_number}: значимых областей не найдено.")

                ocr_page, visual_records = mask_and_extract_visuals(
                    page, page_number, regions, images_dir, work_dir
                )
            except RuntimeError as error:
                print(
                    "Предупреждение: карта/обнаружение областей не сработала; "
                    f"выполняю OCR всей страницы. Причина: {error}"
                )
                regions = []
                visual_records = []
                ocr_page = page

            page_work[page_number] = {
                "regions": regions,
                "visuals": visual_records,
                "ocr_page": ocr_page,
            }

        surya_texts = {}

        if args.ocr == "surya":
            try:
                surya_texts = recognize_pages_surya(
                    [(page_number, page_work[page_number]["ocr_page"])
                     for page_number, _page in pages],
                    surya_dir,
                    temp_dir,
                )
            except RuntimeError as error:
                print(f"Ошибка Surya OCR: {error}")
                print(
                    "Рабочий файл не повреждён. "
                    "Для старого режима можно использовать --ocr tesseract."
                )
                sys.exit(1)

        for sequence, (page_number, page) in enumerate(pages, start=1):
            print(
                f"===== Страница {page_number} "
                f"({sequence} из {len(pages)}) ====="
            )

            regions = page_work[page_number]["regions"]
            visual_records = page_work[page_number]["visuals"]
            ocr_page = page_work[page_number]["ocr_page"]

            if args.ocr == "surya":
                text = surya_texts.get(page_number, "")
            else:
                text = recognize_page(ocr_page, args.lang)

            if args.ocr == "surya" and not args.no_qa:
                try:
                    qa_result = vision_qa_page(
                        ocr_page,
                        page_number,
                        text,
                        vision_dir,
                    )

                    qa_pages.append({
                        "page": qa_result["page"],
                        "agreement": qa_result["agreement"],
                        "surya_words": qa_result["surya_words"],
                        "vision_words": qa_result["vision_words"],
                        "difference_count": len(
                            qa_result["differences"]
                        ),
                    })

                    review_items.extend(
                        qa_result["differences"]
                    )

                    print(
                        "Vision QA: "
                        f"agreement={qa_result['agreement']:.3f}, "
                        f"review={len(qa_result['differences'])}"
                    )

                except RuntimeError as error:
                    print(
                        "Предупреждение: Apple Vision QA не сработал "
                        f"на странице {page_number}: {error}"
                    )

                    qa_pages.append({
                        "page": page_number,
                        "error": str(error),
                    })

            for visual in visual_records:
                image_abs = Path(visual["image"])

                if visual.get("ocr_action") == "exclude":
                    description = (
                        "Описание исключено: область не передаётся в OCR или LLM."
                    )
                elif args.no_descriptions:
                    description = "Описание отключено параметром --no-descriptions."
                else:
                    description = describe_visual(
                        image_abs,
                        args.model,
                        visual["type"],
                    )

                description_path = (
                    descriptions_dir
                    / f"page-{page_number:04d}-image-{visual['index']:02d}.txt"
                )
                description_path.write_text(description, encoding="utf-8")

                visual["description"] = description
                visual["description_file"] = str(
                    description_path.relative_to(output_dir)
                )
                visual["image"] = str(image_abs.relative_to(output_dir))

            records.append({
                "page": page_number,
                "page_image": str(page.relative_to(output_dir)),
                "page_image_absolute": str(page),
                "text": text,
                "visuals": visual_records,
                "ocr_exclusions": [
                    region for region in regions
                    if region.get("ocr_action") == "exclude"
                ],
            })

        if not args.no_pdf:
            create_searchable_pdf(
                records,
                searchable_pdf,
                temp_dir,
                args.lang,
            )

    # TXT
    text_parts = []
    for record in records:
        text_parts.append(
            f"\n\n===== СТРАНИЦА {record['page']} =====\n\n"
            f"{record['text']}"
        )

    text_path.write_text(
        "".join(text_parts).strip() + "\n",
        encoding="utf-8",
    )

    # Markdown
    markdown_parts = [f"# {source.stem}\n"]

    if len(sources) == 1:
        markdown_parts.append(f"\nИсходный файл: `{source.name}`\n")
    else:
        markdown_parts.append("\nИсходные изображения:\n")
        for item in sources:
            markdown_parts.append(f"- `{item.name}`\n")

    markdown_parts.append(f"\nOCR engine: `{args.ocr}`\n")
    markdown_parts.append(f"\nЯзык OCR/PDF: `{args.lang}`\n")

    for record in records:
        markdown_parts.extend([
            f"\n\n## Страница {record['page']}\n",
            "\n### Распознанный текст\n\n",
            record["text"] or "_Текст не обнаружен._",
            "\n",
        ])

        for visual in record["visuals"]:
            markdown_parts.extend([
                f"\n### {visual['type'].capitalize()} "
                f"{visual['index']}\n\n",
                f"![{visual['type']}]({visual['image']})\n\n",
                "**Описание:**\n\n",
                visual.get("description", ""),
                "\n",
            ])

    markdown_path.write_text(
        "".join(markdown_parts),
        encoding="utf-8",
    )

    # JSON — исключаем абсолютные технические пути.
    json_records = []
    for record in records:
        clean_record = dict(record)
        clean_record.pop("page_image_absolute", None)
        json_records.append(clean_record)

    json_path.write_text(
        json.dumps(
            {
                "sources": [str(x) for x in sources],
                "ocr_engine": args.ocr,
                "ocr_languages": args.lang,
                "vision_model": args.model,
                "raw_ocr": args.raw_ocr,
                "qa_enabled": (
                    args.ocr == "surya"
                    and not args.no_qa
                ),
                "qa_pages": qa_pages,
                "pages_filter": args.pages,
                "excluded_pages": args.exclude_pages,
                "region_map": str(args.region_map) if args.region_map else None,
                "pages": json_records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Independent OCR review queue.
    if args.ocr == "surya" and not args.no_qa:
        write_review_outputs(
            review_json_path,
            review_text_path,
            qa_pages,
            review_items,
        )

    # Убираем временную рабочую папку.
    shutil.rmtree(work_dir, ignore_errors=True)

    print()
    print("======================================")
    print("Готово")
    print("======================================")
    print("Markdown:", markdown_path)
    print("Текст:", text_path)
    print("JSON:", json_path)
    print("Страницы:", pages_dir)
    print("Изображения:", images_dir)
    print("Описания:", descriptions_dir)

    if args.ocr == "surya" and surya_dir.exists():
        print("Surya raw output:", surya_dir)

    if args.ocr == "surya" and not args.no_qa:
        if vision_dir.exists():
            print("Vision QA:", vision_dir)

        if review_text_path.exists():
            print("OCR review:", review_text_path)

        if review_json_path.exists():
            print("OCR review JSON:", review_json_path)

    if searchable_pdf.exists():
        print("Поисковый PDF:", searchable_pdf)


if __name__ == "__main__":
    main()
