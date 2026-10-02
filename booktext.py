#!/usr/bin/env python3

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from tools.process_lifecycle import LifecycleRegistry
from tools.review_priority import PRIORITIES, prioritise_review, priority_text
from tools.review_clusters import build_clusters
from tools import document_identity
from tools.owned_workspace import OwnedWorkspace
from tools import chunk_lifecycle
from tools.document_continuity import parse_page_spec


OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "gemma3:4b"
DEFAULT_OCR_LANGUAGES = "rus+eng"
DEFAULT_OCR_ENGINE = "surya"
__version__ = "0.1.0"
SURYA_CLI = Path.home() / "AITools/booktext/benchmark/surya-env/bin/surya_ocr"
VISION_OCR = Path.home() / "AITools/booktext/bin/visionocr"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
VISUAL_TYPES = {"figure", "photo", "diagram", "chart", "map", "table", "illustration", "handwriting"}
EXCLUDED_OCR_REGION_TYPES = {
    "handwriting", "figure", "photo", "diagram", "chart", "map", "illustration",
}
MAX_AUTOMATIC_HANDWRITING_AREA_PERCENT = 20.0


OWNED_CHILDREN = LifecycleRegistry()


def run(command, check=True, capture=False, quiet=False, timeout=None, env=None):
    command = [str(x) for x in command]
    if not quiet:
        print("→", " ".join(command))
    child = OWNED_CHILDREN.spawn(
        command,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=env,
    )
    try:
        stdout, stderr = child.process.communicate(timeout=timeout)
    finally:
        child.cleanup()
    result = subprocess.CompletedProcess(command, child.process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
    return result


def require(command, optional=False):
    if shutil.which(command) is None:
        if optional:
            return False
        print(f"Ошибка: не найдена программа {command}")
        sys.exit(1)
    return True


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


def prepare_pages(sources, pages_dir, temp_dir, include_spec, exclude_spec, *, reuse_existing=False):
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
            if reuse_existing and destination.is_file() and destination.stat().st_size:
                print(f"Повторное использование подготовленного изображения {position}: {image_source.name}")
            else:
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
            if reuse_existing and destination.is_file() and destination.stat().st_size:
                print(f"Повторное использование подготовленной PDF-страницы {page_number}/{total}")
            else:
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
            if reuse_existing and destination.is_file() and destination.stat().st_size:
                print(f"Повторное использование подготовленной DjVu-страницы {page_number}/{total}")
            else:
                print(f"Подготовка DjVu-страницы {page_number}/{total}")
                render_djvu_page(first, page_number, destination, temp_dir)
            pages.append((page_number, destination))

        return pages, total

    if extension in IMAGE_EXTENSIONS:
        selected = choose_pages(1, include_spec, exclude_spec)
        if 1 not in selected:
            raise RuntimeError("Для одиночного изображения доступна только страница 1.")

        destination = pages_dir / "page-0001.png"
        if not (reuse_existing and destination.is_file() and destination.stat().st_size):
            preprocess_image(first, destination)
        return [(1, destination)], 1

    raise RuntimeError(f"Формат {extension} пока не поддерживается.")


def run_native_text_pdf(source, args, started_at):
    """Create a BookText output from an embedded PDF text layer only.

    This is intentionally not an OCR fallback: no page image is rendered and
    no text inside figures or scans is inferred.  The resulting manifest keeps
    per-page source provenance so CDM/export remain the normal production path.
    """
    if source.suffix.lower() != ".pdf":
        raise RuntimeError("native-text-only mode supports PDF input only")
    if shutil.which("pdftotext") is None or shutil.which("pdfimages") is None:
        raise RuntimeError("native-text-only mode requires pdftotext and pdfimages (Poppler)")
    total = get_pdf_page_count(source)
    selected = choose_pages(total, args.pages, args.exclude_pages)
    output_dir = getattr(args, "output_dir", None) or source.parent / f"{source.stem}_book"
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    identity = document_identity.initialize(output_dir, [source])
    images_dir = output_dir / "images"
    images_dir.mkdir(exist_ok=True)
    records = []
    for ordinal, page_number in enumerate(selected, start=1):
        result = run(["pdftotext", "-f", str(page_number), "-l", str(page_number), "-layout", source, "-"], capture=True, quiet=True)
        text = (result.stdout or "").strip()
        prefix = images_dir / f"page-{page_number:04d}-embedded"
        run(["pdfimages", "-png", "-f", str(page_number), "-l", str(page_number), source, prefix], quiet=True)
        visuals = []
        for index, image in enumerate(sorted(images_dir.glob(f"{prefix.name}-*")), start=1):
            visuals.append({
                "index": index, "type": "embedded_pdf_image",
                "image": str(image.relative_to(output_dir)),
                "description": "Embedded PDF image preserved without OCR or interpretation.",
                "source_provenance": {"kind": "pdf_embedded_image", "ocr_performed": False},
            })
        print(f"Нативный текст PDF: страница {ordinal}/{len(selected)}")
        records.append({
            "page": page_number,
            "text": text,
            "visuals": visuals,
            "ocr_exclusions": [],
            "tables": {"status": "NOT_ANALYSED_NATIVE_TEXT_ONLY", "tables": []},
            "source_provenance": {"kind": "pdf_embedded_text", "ocr_performed": False},
        })
    if not any(record["text"] for record in records):
        raise RuntimeError("PDF has no usable embedded text; native-text-only mode refuses to silently fall back to OCR")
    text_path, markdown_path, json_path = output_dir / "book_clean.txt", output_dir / "book.md", output_dir / "book.json"
    text_path.write_text("".join(f"\n\n===== СТРАНИЦА {record['page']} =====\n\n{record['text']}" for record in records).strip() + "\n", encoding="utf-8")
    markdown_path.write_text("# " + source.stem + "\n\nNative PDF text only. Images and figures were not OCR-processed.\n" + "".join(f"\n## Страница {record['page']}\n\n{record['text'] or '_Embedded text not found on this page._'}\n" for record in records), encoding="utf-8")
    json_path.write_text(json.dumps({
        "sources": [str(source)], "ocr_engine": "native_pdf_text", "ocr_performed": False,
        "document": {"title": identity["title"], "identity_artifact": document_identity.FILENAME},
        "native_text_only": True,
        "limitations": ["Text inside images, scans, and figures was intentionally not extracted; embedded PDF images are preserved without OCR."],
        "qa_enabled": False, "qa_pages": [], "pages_filter": args.pages,
        "excluded_pages": args.exclude_pages, "pages": records,
        "page_selection": {"source_total": total, "requested_pages": selected},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    priority = write_review_priority_outputs(output_dir / "review_priority.json", output_dir / "review_priority.txt", [], [], records)
    clusters = build_clusters(priority)
    (output_dir / "review_clusters.json").write_text(json.dumps(clusters, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = build_run_summary(records, total, time.monotonic() - started_at, [], [], priority, clusters, ocr_engine="native_pdf_text", qa_enabled=False)
    summary["native_text_only"] = {"enabled": True, "ocr_processes_started": [], "limitation": "Images and figures were not OCR-processed; extractable embedded PDF images were preserved as source assets."}
    (output_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    document_identity.register_deliverable(output_dir, "book_clean.txt", "OCR text")
    document_identity.register_deliverable(output_dir, "book.md", "OCR Markdown")
    print(f"Папка результата: {output_dir}")
    return output_dir


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


def surya_runtime_environment():
    """Leave room for the image/prompt in each owned llama.cpp context slot.

    Surya's output budget is not a total context budget. In the installed
    Surya2 release both defaults are 12288, which silently cuts dense pages.
    Keep the generation policy unchanged and reserve 8192 input tokens.
    The real journal scan uses 4136 prompt/image tokens, so a 4096 reserve
    alone still falls short. The margin also accommodates other page shapes.
    """
    env = os.environ.copy()
    generation = int(env.get("SURYA_MAX_TOKENS_FULL_PAGE", "12288"))
    requested = int(env.get("SURYA_INFERENCE_CTX_PER_SLOT", "12288"))
    slot = max(requested, generation + 8192)
    env.setdefault("SURYA_MAX_TOKENS_FULL_PAGE", str(generation))
    env["SURYA_INFERENCE_CTX_PER_SLOT"] = str(slot)
    # Surya2 otherwise chooses eight slots on this Mac even for one page.
    # Bound reserved KV memory; explicit operator concurrency is preserved.
    env.setdefault("SURYA_INFERENCE_PARALLEL", "2")
    total = env.get("SURYA_INFERENCE_CTX_SIZE")
    parallel = int(env.get("SURYA_INFERENCE_PARALLEL", "2"))
    if generation <= 0 or requested <= 0 or parallel <= 0:
        raise ValueError("Surya token/context/parallel budgets must be positive")
    if total and int(total) < slot * parallel:
        raise ValueError("SURYA_INFERENCE_CTX_SIZE cannot fit generation plus image/prompt per slot")
    return env


def _recognize_pages_surya_batch(pages, output_dir, temp_dir):
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
        ], env=surya_runtime_environment())
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
        # A page record is mandatory.  Its textual payload is not: a diagram,
        # photograph, or deliberately excluded non-text page can legitimately
        # have no OCR text.  Keep that page in the ordered result as an empty
        # string so the caller can preserve its provenance and continue with
        # the rest of the book.  An absent record is still a fail-closed OCR
        # failure rather than silently manufactured evidence.
        if key not in merged:
            missing.append(page_number)
            continue
        page_data = merged[key]

        if isinstance(page_data, list):
            page_result = page_data[0] if page_data else {}
        elif isinstance(page_data, dict):
            page_result = page_data
        else:
            page_result = {}

        text = _surya_page_text(page_result)

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

    text_pages = sum(bool(text) for text in texts.values())
    print(f"Surya OCR получен для страниц: {len(texts)} (с текстом: {text_pages})")
    return texts


def _atomic_json(path, value):
    """Write a recoverable checkpoint without exposing partial JSON."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_json_object(path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return value if isinstance(value, dict) else default


def _batch_size_for_pages(page_count, explicit_size=None):
    """Compatibility wrapper for the persisted AUTO chunk planning policy."""
    if explicit_size is not None:
        return int(explicit_size)
    return chunk_lifecycle.auto_chunk_size(page_count, operation="surya_ocr")


def _is_resource_failure(error):
    text = str(error).casefold()
    return any(marker in text for marker in ("memory", "out of memory", "timeout", "timed out", "killed", "resource"))


def _surya_payload_from_dir(directory):
    merged = {}
    for result_file in directory.rglob("results.json"):
        value = _load_json_object(result_file, {})
        for key, record in value.items():
            if key in merged:
                raise RuntimeError(f"Surya returned duplicate page payload: {key}")
            merged[key] = record
    return merged


def recognize_pages_surya(pages, output_dir, temp_dir, *, chunk_size=None,
                          source_fingerprint=None, event_callback=None):
    """Run Surya in sequential, resumable persisted processing chunks.

    The chunk plan controls computation and lifecycle only.  Raw Surya evidence
    remains a single canonical page-keyed artifact, so chunks never become a
    semantic or publication boundary.
    """
    if not pages:
        return {}
    require_surya()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "surya-input"
    raw_dir.mkdir(exist_ok=True)
    results_path, checkpoint_path = raw_dir / "results.json", raw_dir / "batch_checkpoint.json"
    merged = _load_json_object(results_path, {})
    selected = [number for number, _page in pages]
    root = output_dir.parent
    fingerprint = source_fingerprint or hashlib.sha256(
        ",".join(str(number) for number in selected).encode("ascii")
    ).hexdigest()
    job_id = "ocr-" + fingerprint[:20]
    runtime = surya_runtime_environment()
    budgets = {key: runtime[key] for key in (
        "SURYA_MAX_TOKENS_FULL_PAGE", "SURYA_INFERENCE_CTX_PER_SLOT", "SURYA_INFERENCE_PARALLEL"
    )}
    if "SURYA_INFERENCE_CTX_SIZE" in runtime:
        budgets["SURYA_INFERENCE_CTX_SIZE"] = runtime["SURYA_INFERENCE_CTX_SIZE"]
    plan = chunk_lifecycle.create_or_load_plan(
        root, job_id=job_id, source_fp=fingerprint, operation="surya_ocr",
        page_numbers=selected, options={"engine": "surya", "raw_schema": "page-keyed-v1", "runtime_budgets": budgets},
        chunk_size=chunk_size,
    )
    # A staged OCR input plus its result can be substantially larger than the
    # source page.  The estimate is conservative and scoped to this managed
    # job; it prevents beginning a chunk that is likely to fill the volume.
    budget = chunk_lifecycle.disk_budget(root, estimated_transient_bytes=plan["chunk_size"] * 50 * 1024 * 1024)
    if not budget["ok"]:
        if event_callback:
            event_callback("cleanup_warning", {"kind": "disk_budget", **budget})
        raise RuntimeError("insufficient managed-workspace disk budget for the next OCR chunk")
    checkpoint = _load_json_object(checkpoint_path, {
        "schema_version": "1.0", "artifact_type": "booktext.adaptive_batch_checkpoint", "completed_pages": [], "events": []
    })
    checkpoint["chunk_plan"] = str(chunk_lifecycle.plan_path(root).resolve().relative_to(root.resolve()))
    checkpoint["initial_batch_size"] = plan["chunk_size"]
    checkpoint["events"] = checkpoint.get("events", [])
    if event_callback:
        event_callback("chunk_plan_created", {"chunks": len(plan["chunks"]), "pages": len(selected), "chunk_size": plan["chunk_size"], "mode": plan["mode"]})
        if any(chunk.get("status") == "complete" for chunk in plan["chunks"]):
            event_callback("job_resumed", {"completed_chunks": sum(chunk.get("status") == "complete" for chunk in plan["chunks"]), "total_chunks": len(plan["chunks"])})
    texts = {}
    page_map = dict(pages)
    for chunk in plan["chunks"]:
        batch_pages = chunk["owned_pages"]
        checkpoint_payload = (
            chunk_lifecycle.valid_checkpoint(root, chunk)
            or chunk_lifecycle.valid_compacted_checkpoint(merged, chunk)
        )
        if checkpoint_payload is not None:
            merged.update(checkpoint_payload)
            for number in batch_pages:
                result = checkpoint_payload[f"page-{number:04d}"]
                result = result[0] if isinstance(result, list) and result else result
                texts[number] = _surya_page_text(result if isinstance(result, dict) else {})
            if event_callback:
                event_callback("chunk_completed", {"chunk_id": chunk["chunk_id"], "pages": batch_pages, "resumed": True})
            continue
        chunk_lifecycle.begin_attempt(root, plan, chunk)
        if event_callback:
            event_callback("chunk_started", {"chunk_id": chunk["chunk_id"], "pages": batch_pages, "current": len(texts), "total": len(selected)})
        batch = [(number, page_map[number]) for number in batch_pages]
        batch_size = len(batch)
        batch_payload = {}
        chunk_texts = {}
        started = time.monotonic()
        position = 0
        while position < len(batch):
            attempt = batch[position:position + batch_size]
            attempt_pages = [number for number, _page in attempt]
            batch_dir = temp_dir / f"surya-batch-{attempt_pages[0]:04d}-{attempt_pages[-1]:04d}-{uuid.uuid4().hex}"
            if event_callback:
                event_callback("chunk_progress", {"chunk_id": chunk["chunk_id"], "pages": attempt_pages, "current": position, "total": len(batch)})
            try:
                # Isolate staged input as well as the Surya child output.
                attempt_texts = _recognize_pages_surya_batch(attempt, batch_dir, batch_dir)
                attempt_payload = _surya_payload_from_dir(batch_dir)
                expected = {f"page-{number:04d}" for number in attempt_pages}
                if set(attempt_payload) != expected:
                    raise RuntimeError("Surya batch payload is incomplete or contains unexpected pages")
            except Exception as error:
                shutil.rmtree(batch_dir, ignore_errors=True)
                if batch_size > 1 and _is_resource_failure(error):
                    reduced = max(1, min(5, batch_size // 2))
                    checkpoint["events"].append({"kind": "automatic_batch_downshift", "from": batch_size, "to": reduced, "pages": attempt_pages, "reason": str(error)})
                    batch_size = reduced
                    _atomic_json(checkpoint_path, checkpoint)
                    if event_callback:
                        event_callback("chunk_retry", {"chunk_id": chunk["chunk_id"], "pages": attempt_pages, "reason": str(error), "next_batch_size": reduced})
                    continue
                chunk_lifecycle.fail_attempt(root, plan, chunk, error)
                checkpoint["last_failure"] = {"pages": attempt_pages, "reason": str(error)}
                _atomic_json(checkpoint_path, checkpoint)
                if event_callback:
                    event_callback("chunk_failed", {"chunk_id": chunk["chunk_id"], "pages": attempt_pages, "reason": str(error)})
                raise
            finally:
                shutil.rmtree(batch_dir, ignore_errors=True)
            batch_payload.update(attempt_payload)
            chunk_texts.update(attempt_texts)
            position += len(attempt)
        try:
            manifest = chunk_lifecycle.commit_checkpoint(
                root, plan, chunk, batch_payload,
                duration_seconds=round(time.monotonic() - started, 3),
                diagnostics={"attempts": chunk["attempts"]},
            )
        except Exception as error:
            chunk_lifecycle.fail_attempt(root, plan, chunk, error)
            raise
        for key, value in batch_payload.items():
            if key in merged:
                # An already-merged raw page can only be reused if byte-for-byte
                # equivalent. This prevents silent duplicate canonical pages.
                if chunk_lifecycle.result_fingerprint({key: merged[key]}) != chunk_lifecycle.result_fingerprint({key: value}):
                    raise RuntimeError(f"duplicate completed page in chunk merge: {key}")
            merged[key] = value
        texts.update(chunk_texts)
        checkpoint["completed_pages"] = sorted(texts)
        checkpoint["last_completed_batch"] = batch_pages
        checkpoint["current_batch_size"] = plan["chunk_size"]
        _atomic_json(results_path, {key: merged[key] for key in sorted(merged)})
        _atomic_json(checkpoint_path, checkpoint)
        if event_callback:
            event_callback("chunk_completed", {"chunk_id": chunk["chunk_id"], "pages": batch_pages, "duration_seconds": manifest["duration_seconds"], "resumed": False})
    if set(texts) != set(selected):
        missing = sorted(set(selected) - set(texts))
        raise RuntimeError("missing completed Surya pages after adaptive batching: " + ", ".join(map(str, missing)))
    validation = {"expected_pages": selected, "completed_pages": sorted(texts), "unique": len(merged) == len(set(merged))}
    if event_callback:
        event_callback("assembly_started", {"pages": len(selected)})
    chunk_lifecycle.mark_assembly(root, plan, complete=True, validation=validation)
    if event_callback:
        event_callback("assembly_completed", validation)
    checkpoint["status"] = "complete"
    checkpoint["completed_pages"] = sorted(texts)
    _atomic_json(checkpoint_path, checkpoint)
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

    try:
        process = run([str(VISION_OCR), str(page)], check=False, capture=True, quiet=True, timeout=120)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Vision QA timed out; owned process tree cleaned") from error

    if process.returncode != 0:
        error = process.stderr.strip()
        raise RuntimeError(
            f"Vision OCR завершился с кодом {process.returncode}: {error}"
        )

    raw = process.stdout

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
            # Vision's normalised rectangles use a bottom-left origin.  Keep
            # that provenance with the raw QA evidence; review clients convert
            # it explicitly to the source-page (top-left) coordinate space.
            "vision_bbox_coordinate_space": "vision_normalized_bottom_left",
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


def extract_structured_tables(surya_dir, tables_dir, page_numbers):
    """Emit deterministic Surya HTML table artefacts without OCR correction."""
    results = surya_dir / "surya-input" / "results.json"
    if not results.exists():
        return {}
    tool = Path(__file__).parent / "tools" / "extract_surya_tables.py"
    run([
        sys.executable, tool, "--results", results,
        "--output-dir", tables_dir,
        "--pages", ",".join(str(page) for page in page_numbers),
    ], quiet=True)
    manifest_path = tables_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {item["page"]: item for item in manifest["pages"]}


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


def write_review_priority_outputs(review_json_path, review_text_path, qa_pages, review_items, records):
    """Write a separate priority layer without changing raw QA evidence."""
    artifact = prioritise_review(qa_pages, review_items, records)
    review_json_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    review_text_path.write_text(priority_text(artifact), encoding="utf-8")
    return artifact


def build_run_summary(
    records,
    total_pages,
    elapsed_seconds,
    qa_pages,
    review_items,
    priority_artifact,
    cluster_artifact=None,
    ocr_engine="surya",
    qa_enabled=True,
    input_source_count=1,
    adaptive_batching=None,
):
    """Build the stable, machine-readable completion result for one run."""
    qa_failures = [item for item in qa_pages if item.get("error")]
    table_entries = [
        table
        for record in records
        for table in (record.get("tables") or {}).get("tables", [])
        if isinstance(table, dict)
    ]
    structured_tables = sum(item.get("status") == "HTML_TABLE" for item in table_entries)
    fallback_tables = sum(item.get("status") == "fallback_review" for item in table_entries)
    visual_count = sum(len(record.get("visuals") or []) for record in records)
    excluded_region_count = sum(len(record.get("ocr_exclusions") or []) for record in records)
    warnings = [
        {
            "kind": "vision_qa_failure",
            "page": item.get("page"),
            "message": item.get("error"),
        }
        for item in qa_failures
    ]
    return {
        "schema_version": "1.0",
        "artifact_type": "booktext.run_summary",
        "booktext_version": __version__,
        "pages": {
            "source_total": total_pages,
            "requested": len(records),
            "processed": len(records),
            "failed": 0,
        },
        "input_source_count": input_source_count,
        "ocr_engine": ocr_engine,
        "adaptive_batching": adaptive_batching,
        "qa_enabled": qa_enabled,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "qa": {
            "pages_checked": len(qa_pages),
            "pages_failed": len(qa_failures),
            "raw_difference_count": len(review_items),
        },
        "review_priority": priority_artifact["counts"],
        "review_clusters": (cluster_artifact.get("metrics") if cluster_artifact else None),
        "tables": {
            "structured": structured_tables,
            "fallback_review": fallback_tables,
        },
        "visuals": {
            "detected_or_extracted": visual_count,
            "ocr_exclusion_regions": excluded_region_count,
        },
        "outputs": {
            "primary_text": "book_clean.txt",
            "markdown": "book.md",
            "manifest": "book.json",
            "raw_review": "review.json" if qa_enabled else None,
            "prioritised_review": "review_priority.json",
            "prioritised_review_text": "review_priority.txt",
            "review_clusters": "review_clusters.json",
            "run_summary": "run_summary.json",
        },
        "warnings": warnings,
        "policy": "Surya remains primary OCR; Apple Vision and table fallback are review evidence only.",
    }


def format_completion_summary(summary, priority_path):
    """Return the compact CLI completion block for a completed run."""
    counts = summary["review_priority"]
    lines = [
        "RUN SUMMARY",
        (
            "Pages processed / failed: "
            f"{summary['pages']['processed']} / {summary['pages']['failed']} "
            f"(source total: {summary['pages']['source_total']})"
        ),
        f"Elapsed: {summary['elapsed_seconds']:.3f}s",
        f"Raw QA differences: {summary['qa']['raw_difference_count']}",
        "Review priority: " + ", ".join(
            f"{priority}={counts.get(priority, 0)}" for priority in PRIORITIES
        ),
        (
            "Tables structured / fallback review: "
            f"{summary['tables']['structured']} / {summary['tables']['fallback_review']}"
        ),
        f"Warnings: {len(summary['warnings'])}",
        f"Prioritised review: {priority_path}",
    ]
    if counts.get("HIGH", 0) == 0:
        lines.append("HIGH review: none.")
    return "\n".join(lines)


def main():
    started_at = time.monotonic()
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
    parser.add_argument("--version", action="version", version=f"booktext {__version__}")

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
        choices=("auto", "surya", "tesseract"),
        default="auto",
        help=(
            "auto (по умолчанию): прямой текст PDF или Surya по содержимому; "
            "surya/tesseract — явные совместимые режимы"
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
        "--chunk-size",
        type=int,
        help=(
            "Размер persisted processing chunk в страницах (1–100). "
            "По умолчанию AUTO: обычно 8, меньше для тяжёлых страниц."
        ),
    )

    # Used only by the versioned machine adapter.  Human CLI output remains
    # readable; machine mode converts these records to protocol JSONL.
    parser.add_argument("--machine-events", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", type=Path, help="Отдельная папка результата; существующие несовместимые данные не перезаписываются")

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
        "--native-text-only",
        action="store_true",
        help="Извлечь только встроенный текст PDF; не рендерить и не OCR-ить изображения или рисунки",
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
    parser.add_argument("--searchable-pdf", action="store_true", help="Явно запросить поисковый PDF в автоматическом режиме")

    parser.add_argument(
        "--no-qa",
        action="store_true",
        help=(
            "Не запускать независимую проверку Surya через Apple Vision "
            "и не создавать очередь review"
        )
    )

    args = parser.parse_args()

    if args.chunk_size is not None and not 1 <= args.chunk_size <= 100:
        parser.error("--chunk-size должен быть в диапазоне от 1 до 100")

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

    from tools import automatic_extraction
    automatic = args.ocr == "auto" and not args.native_text_only
    automatic_pdf = automatic and source.suffix.lower() == ".pdf"
    if automatic:
        args.ocr = "surya"
        if not args.searchable_pdf: args.no_pdf = True
    output_dir = automatic_extraction.output_directory(source, automatic=automatic, explicit=args.output_dir)

    if args.native_text_only:
        try:
            run_native_text_pdf(source, args, started_at)
        except RuntimeError as error:
            print(f"Ошибка native-text-only: {error}")
            sys.exit(1)
        return

    manual_regions = {}
    if args.region_map:
        try:
            manual_regions = load_region_map(args.region_map.expanduser().resolve())
        except RuntimeError as error:
            print(f"Ошибка: {error}")
            sys.exit(1)

    if args.ocr == "surya" and not automatic_pdf:
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

    if not automatic_pdf:
        require("magick")

    if not args.no_pdf:
        require("ocrmypdf")

    if source.suffix.lower() == ".pdf":
        require("pdftoppm")
        require("mutool", optional=True)
        require("qpdf", optional=True)
    if source.suffix.lower() in {".djvu", ".djv"}:
        require("ddjvu")
        require("djvused")

    pages_dir = output_dir / "pages"
    images_dir = output_dir / "images"
    descriptions_dir = output_dir / "descriptions"
    surya_dir = output_dir / "surya"
    vision_dir = output_dir / "vision"
    tables_dir = output_dir / "tables"
    work_dir = output_dir / ".work"

    output_dir.mkdir(parents=True, exist_ok=True)
    identity = document_identity.initialize(output_dir, sources)
    pages_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)
    descriptions_dir.mkdir(exist_ok=True)
    work_dir.mkdir(exist_ok=True)
    tables_dir.mkdir(exist_ok=True)

    source_chunk_fingerprint = None
    reuse_prepared_pages = False
    if args.ocr == "surya":
        # Reuse only images bound to an existing plan for exactly this source.
        # This avoids repeated page rendering/file-provider churn during resume,
        # while a changed source always renders fresh pages and fails stale.
        source_chunk_fingerprint = chunk_lifecycle.source_fingerprint(sources)
        try:
            existing_plan = json.loads(chunk_lifecycle.plan_path(output_dir).read_text(encoding="utf-8"))
            reuse_prepared_pages = (
                isinstance(existing_plan, dict)
                and existing_plan.get("source_fingerprint") == source_chunk_fingerprint
                and existing_plan.get("operation") == "surya_ocr"
            )
        except (OSError, json.JSONDecodeError):
            pass

    markdown_path = output_dir / "book.md"
    text_path = output_dir / "book_clean.txt"
    json_path = output_dir / "book.json"
    review_json_path = output_dir / "review.json"
    review_text_path = output_dir / "review.txt"
    review_priority_json_path = output_dir / "review_priority.json"
    review_priority_text_path = output_dir / "review_priority.txt"
    review_clusters_path = output_dir / "review_clusters.json"
    run_summary_path = output_dir / "run_summary.json"
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

    # A marker-backed workspace makes cleanup ownership explicit. It never
    # performs broad /tmp cleanup and cannot remove user output directories.
    with OwnedWorkspace() as temp_dir:

        try:
            automatic_pages = {}
            if automatic_pdf:
                for dependency in ("pdftotext", "pdfimages", "mutool"):
                    require(dependency)
                selected = choose_pages(get_pdf_page_count(source), args.pages, args.exclude_pages)
                def emit_prepare(details):
                    if args.machine_events:
                        print("BOOKTEXT_CHUNK_EVENT " + json.dumps({"kind": "progress", "details": details}), flush=True)
                automatic_pages, total_pages = automatic_extraction.prepare_pdf(source, output_dir, selected, run, event=emit_prepare,
                    options={"lang": args.lang, "qa": not args.no_qa, "manual_regions": manual_regions, "raw_ocr": args.raw_ocr})
                pages = [(number, data["image"]) for number, data in automatic_pages.items()]
                if any(value["needs_ocr"] or number in manual_regions for number,value in automatic_pages.items()):
                    require_surya()
                    if not args.no_qa: require_vision()
            else:
                pages, total_pages = prepare_pages(
                sources,
                pages_dir,
                temp_dir,
                args.pages,
                args.exclude_pages,
                reuse_existing=reuse_prepared_pages,
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
                elif automatic_pdf:
                    automatic_page = automatic_pages[page_number]
                    page_work[page_number] = {"regions": [], "visuals": automatic_page["visuals"], "ocr_page": automatic_page["ocr_page"]}
                    continue
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
        adaptive_batching = None

        ocr_pages = [(number, page) for number, page in pages
                     if not automatic_pdf or automatic_pages[number]["needs_ocr"] or number in manual_regions]
        if args.ocr == "surya" and ocr_pages:
            try:
                def emit_chunk_event(kind, details):
                    if args.machine_events:
                        print("BOOKTEXT_CHUNK_EVENT " + json.dumps({"kind": kind, "details": details}, ensure_ascii=False, sort_keys=True), flush=True)

                surya_texts = recognize_pages_surya(
                    [(page_number, page_work[page_number]["ocr_page"])
                     for page_number, _page in ocr_pages],
                    surya_dir,
                    temp_dir,
                    chunk_size=args.chunk_size,
                    source_fingerprint=source_chunk_fingerprint,
                    event_callback=emit_chunk_event,
                )
                adaptive_batching = _load_json_object(
                    surya_dir / "surya-input" / "batch_checkpoint.json", None
                )
            except RuntimeError as error:
                print(f"Ошибка Surya OCR: {error}")
                print(
                    "Рабочий файл не повреждён. "
                    "Для старого режима можно использовать --ocr tesseract."
                )
                sys.exit(1)

        table_pages = {}
        if automatic_pdf and ocr_pages:
            from tools.canonical_document import _surya_pages
            raw_pages, _raw_ref = _surya_pages(output_dir)
            for number,_page in ocr_pages:
                if number in manual_regions: continue
                raw_page = raw_pages.get(number, {})
                automatic_extraction.retain_scanned_figures(output_dir, automatic_pages[number], raw_page)
                page_work[number]["ocr_page"] = automatic_pages[number]["ocr_page"]
                if any(str(block.get("label", "")).casefold() == "figure" for block in raw_page.get("blocks", [])):
                    # Figure lettering remains raw evidence, not body text to
                    # translate. Existing explicit OCR modes are unchanged.
                    body_blocks = [block for block in raw_page["blocks"] if str(block.get("label", "")).casefold() != "figure"]
                    surya_texts[number] = _surya_page_text({"blocks":body_blocks})
        if args.ocr == "surya" and ocr_pages:
            try:
                table_pages = extract_structured_tables(
                    surya_dir, tables_dir, [page_number for page_number, _page in ocr_pages]
                )
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                print(f"Предупреждение: структурированные таблицы не извлечены: {error}")

        for sequence, (page_number, page) in enumerate(pages, start=1):
            print(
                f"===== Страница {page_number} "
                f"({sequence} из {len(pages)}) ====="
            )

            regions = page_work[page_number]["regions"]
            visual_records = page_work[page_number]["visuals"]
            ocr_page = page_work[page_number]["ocr_page"]

            native_record = automatic_pages.get(page_number) if page_number not in manual_regions else None
            if args.ocr == "surya":
                text = surya_texts.get(page_number, "")
            else:
                text = recognize_page(ocr_page, args.lang)

            if args.ocr == "surya" and not args.no_qa and (not native_record or native_record["needs_ocr"]):
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

            # QA refers to the OCR-only substring; native text is never a Vision
            # replacement. Mixed-page items cannot silently target another span.
            if native_record and native_record["text"]:
                text = "\n\n".join(part for part in (text, native_record["text"]) if part)

            for visual in visual_records:
                image_abs = Path(visual["image"])

                if visual.get("ocr_action") == "exclude":
                    description = ""
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

            record = {
                "page": page_number,
                "page_image": str(page.relative_to(output_dir)),
                "page_image_absolute": str(page),
                "text": text,
                "visuals": visual_records,
                "ocr_exclusions": [
                    region for region in regions
                    if region.get("ocr_action") == "exclude"
                ],
                "tables": table_pages.get(page_number, {"status": "NOT_APPLICABLE", "tables": []}),
            }
            if native_record:
                record["source_provenance"] = native_record["source_provenance"]
                record["layout_blocks"] = native_record["layout_blocks"]
                record["extraction_warnings"] = native_record["warnings"]
            records.append(record)

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
                "document": {"title": identity["title"], "identity_artifact": document_identity.FILENAME},
                "ocr_engine": "auto" if automatic else args.ocr,
                "extraction_policy": automatic_extraction.POLICY if automatic else "legacy_explicit",
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
                "page_selection": {"source_total": total_pages, "requested_pages": [number for number, _path in pages]},
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

    priority_artifact = write_review_priority_outputs(
        review_priority_json_path,
        review_priority_text_path,
        qa_pages,
        review_items,
        records,
    )
    cluster_artifact = build_clusters(priority_artifact)
    review_clusters_path.write_text(
        json.dumps(cluster_artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    run_summary = build_run_summary(
        records,
        total_pages,
        time.monotonic() - started_at,
        qa_pages,
        review_items,
        priority_artifact,
        cluster_artifact,
        ocr_engine=args.ocr,
        qa_enabled=args.ocr == "surya" and not args.no_qa,
        input_source_count=len(sources),
        adaptive_batching=adaptive_batching,
    )
    if automatic:
        run_summary["extraction_policy"] = automatic_extraction.POLICY
        run_summary["extraction"] = {"ocr_pages": len(ocr_pages), "pages_without_ocr": len(records)-len(ocr_pages),
                                     "native_text_pages": sum(bool(page.get("layout_blocks")) for page in records)}
        run_summary["warnings"].extend(warning for record in records for warning in record.get("extraction_warnings", []))
    run_summary_path.write_text(
        json.dumps(run_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    document_identity.register_deliverable(output_dir, "book_clean.txt", "OCR text")
    document_identity.register_deliverable(output_dir, "book.md", "OCR Markdown")

    # The final raw Surya result is now canonical evidence and all BookText
    # artifacts have been committed.  Compact only verified per-chunk payloads;
    # manifests and canonical raw evidence remain available for audit.
    chunk_cleanup = None
    if args.ocr == "surya" and ocr_pages:
        try:
            if args.machine_events:
                print("BOOKTEXT_CHUNK_EVENT " + json.dumps({"kind": "cleanup_started", "details": {}}, ensure_ascii=False, sort_keys=True), flush=True)
            chunk_cleanup = chunk_lifecycle.compact(output_dir)
            if args.machine_events:
                print("BOOKTEXT_CHUNK_EVENT " + json.dumps({"kind": "cleanup_completed", "details": chunk_cleanup}, ensure_ascii=False, sort_keys=True), flush=True)
        except chunk_lifecycle.ChunkPlanError as error:
            print(f"Предупреждение: chunk cleanup пропущен: {error}")

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
    print("Run summary:", run_summary_path)
    print("Prioritised review:", review_priority_text_path)
    print("Review clusters:", review_clusters_path)

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

    print()
    print(format_completion_summary(run_summary, review_priority_text_path))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "machine":
        from tools.machine_protocol import main as machine_protocol_main
        sys.exit(machine_protocol_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "review":
        from tools.review_workspace import main as review_workspace_main
        review_workspace_main(sys.argv[2:])
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "recover":
        from tools.review_recovery import recover
        import argparse
        parser = argparse.ArgumentParser(prog="booktext recover")
        parser.add_argument("output_dir")
        parser.add_argument("--surya-results", help="preserved Surya results.json used only for source-location evidence")
        options = parser.parse_args(sys.argv[2:])
        print(json.dumps(recover(options.output_dir, surya_results=options.surya_results), ensure_ascii=False, indent=2))
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "document":
        from tools.canonical_document import main as canonical_document_main
        canonical_document_main(sys.argv[2:])
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "export":
        from tools.export_document import main as export_document_main
        export_document_main(sys.argv[2:])
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "translate":
        from tools.translation import translate_document, validate_translation
        parser = argparse.ArgumentParser(prog="booktext translate", description="Create a derived EN↔RU translation from an existing CDM.")
        parser.add_argument("output_dir")
        parser.add_argument("--source-language", required=True, choices=("en", "ru"))
        parser.add_argument("--target-language", required=True, choices=("en", "ru"))
        parser.add_argument("--provider", default="ollama", choices=("ollama",))
        parser.add_argument("--model", required=True, help="Explicit locally installed Ollama model; no hidden default is used")
        parser.add_argument("--glossary", help="JSON glossary mapping source terms to target terms")
        parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
        parser.add_argument("--timeout", type=int, default=120)
        parser.add_argument("--keep-alive", default="30s", help="Bounded Ollama model residency for this translation run (default: 30s)")
        parser.add_argument("--chunk-size", type=int, default=12, help="Whole logical units per resumable translation checkpoint")
        parser.add_argument("--restart", action="store_true", help="Archive current derived translation and restart its owned checkpoints")
        parser.add_argument("--validate", action="store_true", help="Validate existing translation only; do not contact a provider")
        options = parser.parse_args(sys.argv[2:])
        if options.validate:
            result = validate_translation(options.output_dir, source_language=options.source_language, target_language=options.target_language)
        else:
            result = translate_document(options.output_dir, source_language=options.source_language, target_language=options.target_language,
                provider=options.provider, model=options.model, glossary_path=options.glossary, endpoint=options.endpoint, timeout=options.timeout, keep_alive=options.keep_alive, chunk_size=options.chunk_size, restart=options.restart)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    def _cleanup_signal(_signum, _frame):
        OWNED_CHILDREN.handle_signal(_signum, _frame)

    signal.signal(signal.SIGINT, _cleanup_signal)
    signal.signal(signal.SIGTERM, _cleanup_signal)
    try:
        main()
    finally:
        OWNED_CHILDREN.cleanup()
