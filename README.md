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

- единый автоматический путь: встроенный текст PDF → прямое извлечение,
  сканированный текст → Surya, иллюстрации → сохранённые изображения;
- отдельный новый результат `_book_auto`, без перезаписи прежних решений и перевода;
- обратимая нормализация абзацев и диагностические сигналы качества перевода;

- пакетный OCR выбранных страниц через Surya;
- legacy-режим Tesseract;
- TXT, Markdown, JSON и searchable PDF;
- извлечение и описание иллюстраций;
- независимый Apple Vision QA с координатами и confidence;
- отдельный source-anchored слой перевода EN↔RU из CDM с явным локальным
  provider/model, без подмены OCR или финальной книги;
- консервативная реконструкция таблиц частот из уже полученных Surya/Vision
  данных.

## Установка и preflight (macOS)

Автоматическая обработка PDF требует Poppler (`pdftotext`, `pdfimages`,
`pdftoppm`, `pdfinfo`), MuPDF (`mutool`) и Python Pillow. OCR-зависимости нужны
только если обнаружены области распознавания. В приложении выбор OCR-движка
не требуется. Подробности, ограничения и provenance:
[Automatic extraction](docs/architecture/AUTOMATIC_EXTRACTION.md).

Booktext `0.1.0` рассчитан на macOS, особенно Apple Silicon. Версию проверяйте
командой `python3 booktext.py --version`. Требуется Python 3.11+ и ImageMagick
(`magick`) для изображений. Для Surya нужны его изолированный CLI и
`llama-server` из llama.cpp; модели Surya скачиваются локально и занимают
несколько гигабайт диска. Apple Vision helper нужен только для QA, Tesseract —
для режима `--ocr tesseract` и searchable PDF, OCRmyPDF — только если не задан
`--no-pdf`. PDF требует `pdftoppm` (опционально `mutool`/`qpdf` как fallback),
DjVu — `ddjvu` и `djvused`. Ollama требуется только для автоматического поиска
визуальных областей и описаний; с `--raw-ocr` он не нужен.

Установите системные утилиты удобным для вашей системы способом, соберите
Vision helper при необходимости и настройте Surya/llama.cpp по их официальным
инструкциям. Не копируйте локальные пути другого компьютера: CLI сам сообщит
недостающую программу и затронутый режим.

Apple Vision helper собирается с обязательным флагом `-parse-as-library`:

```bash
mkdir -p bin
swiftc -parse-as-library vision/VisionOCR.swift -o bin/visionocr
```

Локальные окружения, бинарники, модели, исходные книги и результаты OCR в Git
не добавляются.

## Production output contract

`book_clean.txt` и `book.md` — выбранный основной OCR-текст и читаемое
представление. `book.json` — машинный page-level manifest и ссылки на assets.
`review.json`/`review.txt` при включённом QA содержат только raw evidence
разногласий Surya/Vision, не автоматические исправления. `review_priority.json`
и `review_priority.txt` — отдельная versioned очередь human review: причины,
сигналы, стабильные идентификаторы и уровни HIGH/MEDIUM/LOW/INFO. Она не меняет
raw review или основной OCR. `run_summary.json` — versioned итог конкретного
запуска: страницы, время, QA, таблицы, предупреждения и counts очереди.
`tables/` хранит
authoritative структурированные артефакты только для native Surya HTML tables;
`table_review/` и `fallback_review` — evidence для человека, не verified cells.
`images/` и `descriptions/` содержат выделенные визуальные области; searchable
PDF создаётся только когда он не нарушит политику исключений.

### Схемы итогов запуска

У `run_summary.json` и `review_priority.json` сейчас `schema_version: "1.0"`.
Первый содержит ключи `pages`, `qa`, `review_priority`, `tables`, `visuals`,
`outputs` и `warnings`; второй — `counts` и упорядоченные `items`. Каждый item
имеет стабильный детерминированный `item_id`, `page`, `priority`, `reasons` и
`signals`; OCR-items также сохраняют отображаемые фрагменты Surya и Vision.
Идентификатор привязан к evidence, а не к позиции в файле, поэтому пригоден как
ключ будущего correction workflow. Версии схем не следует менять задним числом.

### Решения human review и финальная книга

`tools/review_decisions.py` добавляет отдельный, обратимый слой решений. Он
никогда не меняет `book_clean.txt`, `book.md`, `review.json`, `review.txt` или
OCR/table evidence. Поддерживаются `keep_surya`, `accept_vision`, `manual`,
`ignore` и `defer`; для table fallback допустимы только `ignore` и `defer`, без
создания ячеек. Решение привязано к fingerprint review item и raw-текста
страницы: при изменившемся источнике оно не применяется.

```bash
# Найти идентификатор кандидата и записать решение.
python3 tools/review_decisions.py list --priority output/review_priority.json --page 42
python3 tools/review_decisions.py record \
  --priority output/review_priority.json --book output/book.json \
  --decisions output/review_decisions.json --item qa-p0042-... \
  --action manual --replacement "Проверенный текст"

# Пересобрать отдельную финальную редакцию исключительно из raw OCR + decisions.
python3 tools/review_decisions.py build \
  --priority output/review_priority.json --book output/book.json \
  --decisions output/review_decisions.json --output-dir output
```

Build не запускает Surya, Vision, Tesseract, Ollama или другую модель. Он пишет
`book_final.txt`, `book_final.md` и `final_build.json`; последний содержит
fingerprints входов, статусы решений, unresolved HIGH/MEDIUM и stale/conflict
counts. v1 Markdown воспроизводится из authoritative `book.json`, сохраняя
безопасные ссылки на visuals и tables, а не патчит старый Markdown по позиции.

### Локальное review workspace

Для большой очереди не нужно открывать JSON вручную. Запустите только на
локальном компьютере:

```bash
python3 booktext.py review /path/to/book_output
```

Команда требует `book.json` и `review_priority.json`, слушает только
`127.0.0.1` и печатает URL. Workspace лениво загружает один item и его страницу,
показывает Surya/Vision evidence, причины, QA-сигналы, current decision и
progress. Фильтры и навигация по умолчанию ставят unresolved HIGH перед MEDIUM,
LOW и INFO. Решения autosave в атомарный `review_decisions.json` через тот же
validated decision layer; browser restart их не теряет. В интерфейсе есть
безопасная кнопка final build, использующая тот же локальный #33 workflow и не
запускающая OCR/LLM. Никакие данные не отправляются по сети.

`review_clusters.json` — отдельный versioned organizational view очереди. Он
сохраняет каждый item ровно один раз и помогает раскрывать повторяющиеся
безопасные patterns, но не создаёт решений и не имеет «apply to all». Числа,
дроби, единицы, Latin/scientific terms, completeness и table fallback намеренно
остаются отдельными. В workspace переключатель Items/Clusters показывает
members и их независимые decision statuses.

Для очень узкого набора кластеров workspace также показывает batch review. Он
доступен только для multi-member `repeated_lexical_pair` с точно одинаковой
Cyrillic Surya→Vision парой, priority и reasons; numeric/Latin, structural,
table и неоднозначные случаи исключены. Reviewer видит полный список участников,
может исключить отдельных участников, затем обязан отдельно подтвердить preview.
Batch создаёт обычное решение для каждого оставшегося item в атомарной записи
`review_decisions.json` с provenance; это не «apply to all». Допустимы только
`keep_surya`, `accept_vision` и `ignore`; batch можно безопасно откатить по его
provenance.

Ограничения: локальные OCR-ошибки возможны; disagreement двух движков — повод
для review, а не доказательство ошибки; image-only и ambiguous tables требуют
human review и никогда не становятся подтверждёнными ячейками автоматически.

## Основной CLI

### Canonical Document Model

После OCR и human decisions можно детерминированно собрать каноническую
структуру для будущих exporters и BookText.app, не запуская OCR или LLM:

```bash
python3 booktext.py document build /path/to/book_output
python3 booktext.py document validate /path/to/book_output/book_document.json \
  --output-dir /path/to/book_output
```

Результат — versioned `book_document.json`; правила и поля описаны в
[`docs/architecture/CANONICAL_DOCUMENT_MODEL.md`](docs/architecture/CANONICAL_DOCUMENT_MODEL.md).
Неуверенная структура сохраняется как `unknown`, а fallback tables не содержат
придуманных ячеек.

### Publication exports from CDM

Экспорт создаёт human-facing formats только из `book_document.json`; CDM
остаётся machine source для будущей Integra Evidence/Knowledge цепочки:

```bash
python3 booktext.py export /path/to/book_output --format all
```

Нужно выбрать один или несколько форматов; `--format all` — явный выбор всех.
Доступны Markdown, local HTML, DOCX, EPUB, publication PDF и raster PDF без
выделяемого текста (`--format pdf_image`), а неявного экспорта всех форматов
нет. Команда создаёт только выбранные готовые файлы и
`exports/export_manifest.json`. В manifest записаны hash/size/validation,
unresolved review и fallback/unknown counts. Подробный contract и локальные
зависимости: [`docs/architecture/PUBLICATION_EXPORTS.md`](docs/architecture/PUBLICATION_EXPORTS.md).

### Производный перевод EN↔RU

Перевод запускается только явно и только от уже проверенного CDM. В первой
версии поддержан локальный Ollama с единственной моделью перевода `translategemma:4b` без облачного fallback:

```bash
python3 booktext.py translate /path/to/book_output \
  --source-language en --target-language ru \
  --provider ollama
```

Результат находится в `translations/`: block-level provenance, модель и
настройки, исходный и переводной текст, а также target/bilingual Markdown.
В приложении либо через machine protocol можно отдельно экспортировать уже
валидный перевод в один или несколько выбранных форматов (Markdown, HTML,
DOCX, EPUB, publication PDF и raster PDF), без повторного OCR или обращения
к модели:

```bash
python3 booktext.py machine translation-export /path/to/book_output \
  --source-language en --target-language ru --format docx
```
`--validate` проверяет staleness без обращения к модели. Подробный контракт:
[`docs/architecture/TRANSLATION_LAYER.md`](docs/architecture/TRANSLATION_LAYER.md).

### Native macOS app and machine protocol

The native SwiftUI app presents a document-centric workflow: choose a document,
follow processing progress, review only when required, then open, translate, or
export the ready book. Core configuration, processing options, diagnostics,
and manual storage cleanup remain under **Settings** instead of the routine
document screen.

Before pressing **Start**, the app can save a simple sequential workflow:
process, choose a review policy, optionally translate EN↔RU, then export only
the selected formats. After one Start, Core advances validated stages in order;
an interruption resumes at the first incomplete stage without repeating valid
OCR or translation checkpoints. This is a fixed product workflow, not a general
automation or scripting feature.

The app uses the versioned JSONL contract, never terminal prose:

```bash
python3 booktext.py machine preflight --operation export
python3 booktext.py machine inspect /path/to/book_output
python3 booktext.py machine project-history
python3 booktext.py machine project-remove JOB_ID
python3 booktext.py machine export /path/to/book_output --format html
python3 booktext.py machine translation /path/to/book_output \
  --source-language en --target-language ru
python3 booktext.py machine translation-export /path/to/book_output \
  --source-language en --target-language ru --format docx
```

Commands, stable events, exit codes, cancellation, and logical artifact
discovery are documented in
[`docs/architecture/MACHINE_PROTOCOL.md`](docs/architecture/MACHINE_PROTOCOL.md).

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

Для TXT, Markdown, RTF, DOCX, цифрового PDF и DjVu со скрытым текстовым слоем
сначала используйте native extraction. Пустая страница или неподдержанный
формат явно получают статус `OCR_REQUIRED`, а не создают пустой текст под видом
успешного результата. У PDF и DjVu поддерживается тот же выбор физических
страниц:

```bash
python3 tools/extract_native_text.py source.docx --output native-text.json
python3 tools/extract_native_text.py source.djvu --pages 5,7,11 --exclude-pages 7 --output native-text.json
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
./scripts/test_suite.sh
```

Temporary lifecycle and evidence retention: [owned TMP audit](docs/OWNED_TMP_LIFECYCLE.md). Use `scripts/test_suite.sh` for interruption-safe tests; historical diagnostics require `BOOKTEXT_DIAGNOSTIC_DIR` outside TMP.

Central activated-release registration and the post-Activation publisher are
documented in [Central activation publication](docs/architecture/CENTRAL_ACTIVATION_PUBLICATION.md).
