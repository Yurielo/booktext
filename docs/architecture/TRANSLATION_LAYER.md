# Derived translation layer

BookText translation is a separate, versioned derivative of an existing
`book_document.json` (CDM).  It is not an OCR correction mechanism and never
rewrites raw OCR, QA evidence, review decisions, final-book artifacts, or CDM.

## Explicit local invocation

The first supported provider is a caller-selected local Ollama model.  There
is deliberately no default model and no cloud fallback:

```bash
python3 booktext.py translate OUTPUT_DIR \
  --source-language en --target-language ru \
  --provider ollama --model qwen3:8b
```

The reverse direction uses `--source-language ru --target-language en`.  A
JSON glossary can be supplied with `--glossary glossary.json`; it maps source
terms to required target terms and is fingerprinted in the result.

`booktext machine translation …` exposes the same operation to BookText.app
or another local client. `booktext machine translation-validate …` checks an
existing result only and never contacts Ollama.

## Artifacts and safety

For EN→RU, BookText writes:

- `translations/en-ru.json`: block-level source anchors, original and target
  text, provider/model/settings provenance, glossary fingerprint and status;
- `translations/book-en-ru.md`: target-language reading projection;
- `translations/book-en-ru-bilingual.md`: target text with original text
  alongside it;
- `translations/history/`: preserved earlier derived revisions when the CDM
  changed.

Only textual CDM blocks participate. Page breaks, figures, image-only table
fallbacks and excluded manuscript content never become invented translated
text. A block already entirely in the target language is explicitly recorded
as skipped rather than sent to a model. Mixed-language blocks are still
source-anchored and their direction is recorded.

The layer verifies numeric tokens before a translation receives
`machine_translated` status. A changed number, fraction or compact unit yields
`needs_numeric_review`; it never becomes human-reviewed automatically.

Each result fingerprints its source CDM and every source block. If the CDM or
a participating block changes, validation reports `stale: true`; a new run
preserves the old derived revision before atomically publishing the new one.

Machine translation is evidence for a later human language review, separate
from OCR human review. Cloud and paid translation services are intentionally
out of scope until the Product Owner explicitly chooses one.
