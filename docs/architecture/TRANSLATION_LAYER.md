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

BookText asks thinking-capable local models for a non-reasoning response
(`think: false`): translation requires the rendered block, not an internal
reasoning trace. This setting is stored in the per-block provider provenance.

Each BookText translation request asks Ollama for a bounded `keep_alive: 30s`
lease. Sequential blocks reuse the loaded model during that lease; after the
last BookText request Ollama unloads the model naturally when the lease
expires. BookText never calls a global unload, stops `ollama serve`, or kills a
process by name, because that could interrupt another local client. The bounded
lease applies on normal completion, provider failure, timeout, and cancellation
once Ollama finishes the already-received request. Advanced CLI and machine
callers may pass a bounded `--keep-alive` value such as `30s`; BookText accepts
only 1 ms through 300 s and defaults to 30 s.

## Artifacts and safety

For EN→RU, BookText writes:

- `translations/en-ru.json`: block-level source anchors, original and target
  text, provider/model/settings provenance, glossary fingerprint and status;
- `translations/book-en-ru.md`: target-language reading projection;
- `translations/book-en-ru-bilingual.md`: target text with original text
  alongside it;
- `translations/history/`: preserved earlier derived revisions when the CDM
  changed.

The validated derivative can be exported without re-running OCR or a model:

```bash
booktext machine translation-export OUTPUT_DIR \
  --source-language en --target-language ru --format docx
```

One or more publication formats can be selected explicitly.  Generated files
and their separate manifest live under
`translations/exports/en-ru/`; ordinary book exports remain untouched.  This
export uses an in-memory translated CDM projection built from the validated
translation anchors.  It never changes canonical CDM, raw OCR, or review
artifacts.

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

## Resumable logical-unit execution

Translation is checkpointed below `translations/chunks/`, separately from OCR
page chunks.  A translation chunk owns complete CDM text-block identifiers,
never a physical page slice.  It records adjacent blocks only as read-only
context.  Context is sent to the local model to improve continuity but is
never committed or assembled as output.

For every owned chunk BookText performs:

`translate → validate → atomic checkpoint → verify → release transient request state`.

Completed, hash-verified chunks are resumed without provider calls.  The final
assembly checks exact source-unit coverage, duplicate absence, source order,
and context-only exclusion before publishing the versioned translation
artifact.  After successful assembly, checkpoint payloads are compacted while
the source-anchored final artifact, plan metadata, and provenance remain.

`--chunk-size` is measured in complete logical units, not pages.  `--restart`
is explicit: it archives an existing derived translation before restarting its
authenticated checkpoint workspace.  The machine `translation` operation emits
chunk plan/start/completion/resume/assembly/cleanup JSONL events for native
progress UI.
