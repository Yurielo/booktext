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

### Выбор страниц

`--pages` запускает извлечение и OCR только для указанных страниц. Поддерживаются
одна страница, список через запятую и диапазоны; их можно сочетать:

```bash
# только страница 5
python3 booktext.py --pages 5 source.pdf

# только страницы 5, 7 и 11
python3 booktext.py --pages 5,7,11 source.pdf

# страницы 5–11 и 20
python3 booktext.py --pages 5-11,20 source.pdf
```

`--exclude-pages` обрабатывает весь файл, кроме указанных страниц. Синтаксис тот
же, а флаг можно сочетать с `--pages`:

```bash
# всё, кроме страниц 5, 7 и 11
python3 booktext.py --exclude-pages 5,7,11 source.pdf

# всё, кроме диапазона 5–11
python3 booktext.py --exclude-pages 5-11 source.pdf

# из диапазона 1–20 пропустить 5, 7 и 11
python3 booktext.py --pages 1-20 --exclude-pages 5,7,11 source.pdf
```

Отключить независимую проверку Vision можно флагом `--no-qa`. Полный список
параметров:

```bash
python3 booktext.py --help
```

### Исключение рукописи и текста внутри иллюстраций

Booktext маскирует рукописные пометки и выбранные иллюстрации **до** Surya,
Apple Vision QA и Tesseract. Автоматическое обнаружение помогает построить
карту, но для исправления границ можно передать проверяемый JSON. Наличие
записи страницы (даже с пустым `regions`) заменяет автоматическое решение для
этой страницы.

```json
{
  "schema_version": 1,
  "pages": [{
    "page": 5,
    "regions": [{
      "type": "handwriting",
      "action": "exclude",
      "bbox_percent": [70, 4, 95, 15]
    }]
  }]
}
```

```bash
python3 booktext.py --region-map reviewed-regions.json source.pdf
```

Допустимы типы `handwriting`, `figure`, `photo`, `diagram`, `chart`, `map` и
`illustration`. Исключённые фрагменты сохраняются как визуальные вложения, но
не отправляются в OCR или LLM-описание. Пока поисковый PDF не умеет создавать
текстовый слой из той же карты, он при таких исключениях не выпускается — это
предотвращает возвращение исключённого текста независимым OCRmyPDF.

### Извлечение уже существующего текста

Для TXT, Markdown, RTF, DOCX и цифрового PDF сначала используйте native
extraction. Пустая страница или неподдержанный формат явно получают статус
`OCR_REQUIRED`, а не создают пустой текст под видом успешного результата:

```bash
python3 tools/extract_native_text.py source.docx --output native-text.json
```

## Консервативное извлечение таблиц

Извлечение использует существующий `surya/surya-input/results.json` и не
запускает OCR повторно. Оно сохраняет исходный HTML, развёрнутую grid-таблицу,
границы и confidence Surya. Значения не исправляются и не заменяются.

```bash
python3 tools/extract_surya_tables.py \
  --results /path/to/book-output/surya/surya-input/results.json \
  --output-dir /path/to/table-output \
  --pages 305,306,320,321
```

`manifest.json` не содержит абсолютный путь к исходнику: вместо него хранится
имя и SHA-256. Страницы без найденной таблицы или без результата также явно
попадают в manifest.

Для страниц, где нужно сопоставить структуру Surya с независимым распознаванием
Apple Vision, соберите пакет ручной проверки:

```bash
python3 tools/build_table_review_package.py \
  --page 320 \
  --data-root /path/to/book-output \
  --output-dir /path/to/table-review
```

Результат намеренно имеет `review_required: true`: он хранит исходную HTML
таблицу, её сетку и счётчики структурных свидетельств Vision, но не выдаёт
несверенные ячейки за достоверный текст.

`tools/reconstruct_frequency_table.py` остаётся прежним прототипом сопоставления
Surya и Vision. Перед повторным включением в основной путь он должен пройти
контрольные страницы 305, 306, 320 и 321; текущие экспериментальные версии не
используются автоматически.

## Канонический пакет документа

Для уже полученного `book.json` можно сформировать переносимые `document.json`
и `manifest.json`, не копируя абсолютные пути в артефакты:

```bash
python3 tools/build_document_package.py \
  --book-json /path/to/book-output/book.json \
  --source /path/to/original.pdf \
  --surya-results /path/to/book-output/surya/surya-input/results.json \
  --review-json /path/to/book-output/review.json \
  --output-dir /path/to/document-package
```

Пакет создаёт стабильные идентификаторы источников, документа, ревизии, страниц,
блоков и ячеек. Для Surya-блоков он сохраняет исходный HTML, координаты, порядок
чтения и выбранный машинный текст. Если таких данных нет, это явно отмечается в
`limitations`, а не заменяется недоказанным происхождением.

Для набора фотографий передайте все исходные изображения в их исходном порядке,
даже если OCR запускался только для части страниц. Тогда, например, страницы
`5,7,11` сохранят ссылки именно на пятый, седьмой и одиннадцатый исходники.

Для набора фотографий до OCR зафиксируйте ориентацию оригиналов:

```bash
python3 tools/inspect_photo_set.py --source IMG_1275.JPG --source IMG_1276.JPG \
  --output photo-set.json
```

Manifest сохраняет хеши, исходную геометрию, EXIF-ориентацию и результат
`imagemagick:auto-orient`; повёрнутые снимки получают `review_required: true`.

## RTF для human review

```bash
python3 tools/build_review_rtf.py --book-json /path/to/book.json \
  --review-json /path/to/review.json --output review.rtf --report review-rtf.json
```

RTF подсвечивает жёлтым фрагменты исходного Surya OCR, для которых есть
разногласие с Vision. Он не подменяет их текстом Vision; несовпадения, которые
невозможно точно связать с фрагментом, остаются в `review-rtf.json`.

После ручного редактирования копии RTF можно получить только предложения
правок, не меняя OCR автоматически:

```bash
python3 tools/diff_review_rtf.py --baseline review.rtf --reviewed reviewed.rtf \
  --output review-edit-candidates.json
```

Когда `reviewed.rtf` является завершённой human review, импорт создаёт новую
версию текста и неизменяемый журнал принятых решений, не затрагивая raw OCR:

```bash
python3 tools/import_reviewed_rtf.py --baseline review.rtf --reviewed reviewed.rtf \
  --text-output human-reviewed.txt --log-output human-review-import.json
```

Чтобы связать завершённую human review с новой ревизией `document.json`, не
переписывая raw OCR, используйте:

```bash
python3 tools/apply_human_review_to_document.py \
  --document document.json --reviewed-text human-reviewed.txt \
  --review-import human-review-import.json --output document-reviewed.json
```

Файлы `tools/hybrid_*` и `tools/visual_recheck_v18.py` сохранены как история
экспериментов. Они не входят в текущую production-архитектуру и не должны
использоваться для автоматического OCR arbitration.

## Проверка

```bash
python3 -m py_compile booktext.py tools/extract_surya_tables.py
python3 -m unittest discover -s tests -v
```
