# Canonical Document Model (CDM)

`book_document.json` is BookText's versioned, deterministic structural contract
between OCR/review evidence and future exporters or the native application.
Its current schema is `booktext.canonical_document` version `1.0`.

The model is deliberately conservative. `book.json` remains authoritative raw
OCR; valid decisions are applied using the existing decision layer only. The
CDM does not run OCR or a model and never replaces an uncertain structure with
a semantic guess.

## Top-level fields

- `metadata`: known source facts; title/author are `null` unless explicitly
  supplied by a source artifact.
- `source`: fingerprints of BookText artifacts used for the projection.
- `source_pages`: source-page and prepared-image/raw-OCR provenance.
- `blocks`: globally ordered blocks with stable deterministic IDs.
- `review`: final-build decision status and unresolved HIGH/MEDIUM counts.
- `notes`: reserved, empty unless an evidence-backed note relationship exists.
- `warnings`: unavailable declared assets; those are not emitted as valid refs.

Supported block types are `heading`, `paragraph`, `list`, `table`, `figure`,
`caption`, `page_break`, and `unknown`. Heading levels and captions are `null`
or absent unless layout evidence supports them. Unknown blocks preserve text.

Structured tables reference their verified JSON/TSV/raw-HTML artifacts. A
fallback table has `table_status: fallback_review` and deliberately has no
`cells` field. Figure descriptions are auxiliary metadata, never a substitute
for an image or caption.

## Build and validation

```bash
python3 booktext.py document build /path/to/book_output
python3 booktext.py document validate /path/to/book_output/book_document.json \
  --output-dir /path/to/book_output
```

The build only reads existing artifacts and atomically writes
`book_document.json`. Validation fails for an unsupported schema, duplicate
IDs, invalid page/reading provenance, broken references, missing table files,
invalid decision provenance, or unsupported block types.
