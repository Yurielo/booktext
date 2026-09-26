# Booktext

Локальный конвейер для оцифровки PDF, DjVu и изображений на русском и
английском языках. Основной OCR выполняет Surya, а Apple Vision используется
как независимый QA-источник. Разногласия не подменяют основной текст
автоматически: они сохраняются в очереди review.

## Целевой результат и план

Обязательная цель дальнейшей разработки — переносимые результаты, пригодные
для включения в Knowledge-процесс Integra с сохранением источников, структуры
и статуса проверки. Совместимость пока не реализована и должна подтверждаться
сквозным импортом в приложение.

Этапы, зависимости и критерии готовности описаны в
[плане разработки](DEVELOPMENT_PLAN.md).
Контракт будущего адаптера описан в
[Booktext → Integra OCR adapter contract](docs/INTEGRA_OCR_ADAPTER_CONTRACT.md).

## Возможности

- пакетный OCR выбранных страниц через Surya;
- legacy-режим Tesseract;
- TXT, Markdown, JSON и searchable PDF;
- извлечение и описание иллюстраций;
- независимый Apple Vision QA с координатами и confidence;
- консервативная реконструкция таблиц частот из уже полученных Surya/Vision
  данных.

## Требования

Проект рассчитан на macOS/Apple Silicon. Основной CLI ожидает внешние утилиты
`magick`, `pdftoppm`, `ddjvu`, `djvused`, `tesseract`, `ocrmypdf` и локально
установленный Surya CLI с `llama-server`. Для описания иллюстраций может
использоваться Ollama.

Apple Vision helper собирается с обязательным флагом `-parse-as-library`:

```bash
mkdir -p bin
swiftc -parse-as-library vision/VisionOCR.swift -o bin/visionocr
```

Локальные окружения, бинарники, модели, исходные книги и результаты OCR в Git
не добавляются.

## Основной CLI

```bash
python3 booktext.py --ocr surya --pages 1-20 source.pdf
```

Отключить независимую проверку Vision можно флагом `--no-qa`. Полный список
параметров:

```bash
python3 booktext.py --help
```

## Реконструкция frequency-таблиц

Прототип использует существующий `surya/surya-input/results.json` и
`vision/page-NNNN.json`; повторный OCR не запускается.

```bash
python3 tools/reconstruct_frequency_table.py \
  --page 320 \
  --data-root /path/to/book-output
```

По умолчанию создаются `/tmp/page-0320-table.json` и
`/tmp/page-0320-table.tsv`. Каждая ячейка содержит primary-значение Surya,
значение Vision, источник решения, confidence, bbox и `review_required`.
Совпадение подтверждается только независимым согласием OCR; при расхождении
сохраняется primary-значение Surya и выставляется review-флаг.

Файлы `tools/hybrid_*` и `tools/visual_recheck_v18.py` сохранены как история
экспериментов. Они не входят в текущую production-архитектуру и не должны
использоваться для автоматического OCR arbitration.

## Проверка

```bash
python3 -m py_compile booktext.py tools/reconstruct_frequency_table.py
python3 -m unittest discover -s tests -v
```
