# Derived translation layer

BookText translation is a separate, versioned derivative of an existing
`book_document.json` (CDM).  It is not an OCR correction mechanism and never
rewrites raw OCR, QA evidence, review decisions, final-book artifacts, or CDM.

## Explicit local invocation

BookText Core supports one production translation model: `translategemma:4b` in local Ollama. The official TranslateGemma prompt uses one user message, explicit English/Russian language names and codes, two blank lines before owned source text, and a plain-text translation-only response. BookText adds labelled read-only document and neighbouring-block context and applicable glossary terms. It does not request JSON or model thinking. [Ollama model and prompt guide](https://ollama.com/library/translategemma).

```bash
python3 booktext.py translate OUTPUT_DIR \
  --source-language en --target-language ru
```

An explicit `--model` is accepted only when it is exactly `translategemma:4b`. A retired model request fails before inference with `MODEL_RETIRED`. If an old Qwen checkpoint exists, BookText reports `MIGRATION_REQUIRED`; run the command again with `--migrate` to archive the old checkpoint workspace and start a new translation from saved CDM. The old completed translation remains available for opening and export without Ollama.

For a legacy pipeline, use `booktext machine pipeline-migrate-translation OUTPUT_DIR` and then `booktext machine pipeline-resume OUTPUT_DIR`. Core preserves completed processing, review and CDM stages. A repeated migration command is idempotent. A historical revision can be exported offline with `booktext machine translation-export OUTPUT_DIR --source-language en --target-language ru --history-sha SHA256 --format docx`; the SHA is the suffix of `translations/history/en-ru-SHA256.json`. Historical exports are written under `translations/exports/en-ru/history/SHA256/` and do not overwrite the current translation export.

The reverse direction uses `--source-language ru --target-language en`.  A
JSON glossary can be supplied with `--glossary glossary.json`; it maps source
terms to required target terms and is fingerprinted in the result.
Glossary values may also be `{ "target": "…", "variants": ["…"] }`, so
approved Russian inflections or alternatives can be checked without rewriting
the model's sentence.  A repeatable bounded pilot can use one or more
`--block-id` values; those units retain their original document order and use
the full CDM only for read-only neighbouring context.

`booktext machine translation …` exposes the same operation to BookText.app
or another local client. `booktext machine translation-validate …` checks an
existing result only and never contacts Ollama.

BookText accepts a requested idle `--keep-alive` from 1 ms through 300 s,
defaulting to 30 s. Core raises the effective request lease to cover bounded
request retries: with the default 120 s timeout and three request attempts,
the effective Ollama lease is capped at 300 s. Retry timeouts are 120, 240
and 300 s, so a failing unit can take up to 660 s of request waiting per
quality attempt; short units permit at most three quality attempts. The
effective lease is recorded in provider provenance. Sequential blocks reuse
the loaded model; after the final request Ollama releases it when the lease
expires, unless another client extends the lease. BookText never stops the
shared daemon or unloads a model by process name. A per-user file lock admits
one BookText translation worker at a time and releases on failure or
cancellation.

## Artifacts and safety

For EN→RU, BookText writes:

- `translations/en-ru.json`: block-level source anchors, original and target
  text, provider/model/settings provenance, glossary fingerprint and status;
- `translations/book-en-ru.md`: target-language reading projection;
- `translations/book-en-ru-bilingual.md`: target text with original text
  alongside it;
- `translations/history/`: content-addressed earlier manifests, readable
  Markdown projections, prior exports and checkpoint workspaces.

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

The layer separates request/response success, checkpoint integrity, automated
quality signals and human semantic acceptance. It checks numeric tokens,
provider completion reason, mixed-script words, repeated output, unchanged
headings, applicable glossary terms and possible read-only-context leakage.
A changed number, fraction or compact unit yields `needs_numeric_review`; an
unresolved quality signal yields `needs_quality_review`. Neither state becomes
human-reviewed automatically. A clean `machine_translated` state is still a
machine result, not semantic acceptance.

Each result fingerprints its source CDM and every source block. If the CDM or
a participating block changes, validation reports `stale: true`; a new run
preserves the old derived revision before atomically publishing the new one.

Machine translation is evidence for a later human language review, separate
from OCR human review. Cloud and paid translation services are intentionally
out of scope until the Product Owner explicitly chooses one.

`tools/hybrid_context_v13.py`, `tools/hybrid_semantic_audit_v15.py` and
`tools/hybrid_targeted_audit_v16.py` are archived OCR audit experiments. They
are not imported by the app, production Core, pipeline, bootstrap or current
acceptance automation. Their Qwen references remain as historical research
provenance and are not TranslateGemma translation paths.

## Resumable logical-unit execution

Translation is checkpointed below `translations/chunks/`, separately from OCR
page chunks.  A translation chunk owns complete CDM text-block identifiers,
never a physical page slice.  It records adjacent blocks only as read-only
context.  Context is sent to the local model to improve continuity but is
never committed or assembled as output.

For every owned chunk BookText performs:

`translate → validate → atomic checkpoint → verify → release transient request state`.

Each completed logical unit is atomically retained as a verified prefix while
its chunk is running. Therefore a failure in a neighbouring unit does not
retranslate completed units on resume. Completed, hash-verified chunks are
also resumed without provider calls. The final assembly proves exact
source-unit coverage, duplicate absence and source order; it explicitly marks
read-only-context exclusion as `NOT_SEMANTICALLY_VERIFIED`, because ID coverage
alone cannot prove semantic correctness. After successful assembly, checkpoint
payloads are compacted while the source-anchored final artifact, plan metadata,
attempts and provenance remain.

`--chunk-size` is measured in complete logical units, not pages.  `--restart`
is explicit: it archives an existing derived translation before restarting its
authenticated checkpoint workspace.  The machine `translation` operation emits
chunk plan/start/completion/resume/assembly/cleanup JSONL events for native
progress UI.
