# Publication exports from CDM

Publication output is built only from `book_document.json` and its verified
local references. The CDM remains the canonical machine source for the future
BookText CDM → Integra Evidence → Integra Knowledge path; Markdown, HTML,
DOCX, EPUB and PDF are human-consumption projections only.

```bash
python3 booktext.py export /path/to/book_output --format all
python3 booktext.py export /path/to/book_output --format html --format epub
```

Outputs are written under `exports/`:

- the explicitly requested `book.md`, `book.html`, `book.docx`, `book.epub`,
  `book.pdf`, or `book-image.pdf`;
- copied local figure assets where needed;
- `export_manifest.json`.

The manifest contains the CDM fingerprint, BookText version, each generated
file's relative path/hash/size, validation state, inherited warnings,
unresolved HIGH/MEDIUM counts, and unknown/fallback counts.

Verified tables read only their CDM-referenced JSON grid. A fallback table is
rendered as explicit review content and has no fabricated cells. Unknown blocks
remain explicit in every textual format. Export fails instead of treating a
missing/corrupt CDM, table artifact, archive, local HTML/Markdown asset, or PDF
as success.

DOCX and EPUB use local Pandoc. The publication PDF uses a local Python with
`reportlab`; set `BOOKTEXT_EXPORT_PYTHON` if the Python running BookText does
not provide it. Neither route starts OCR, Vision, Tesseract, Ollama, or an LLM.

At least one `--format` is required. `--format all` is explicit; BookText never
silently exports every format. `pdf_image` is a rasterized copy of the
CDM-derived publication PDF with no selectable text (it is not a facsimile of
the original source pages) and requires local Poppler `pdftoppm`.
