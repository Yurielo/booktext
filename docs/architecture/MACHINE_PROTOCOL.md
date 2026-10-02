# BookText Core machine protocol

`booktext machine` is the versioned local contract between BookText Core and a
future native BookText.app. It is not a network service and it never accepts
shell fragments: input paths are ordinary argv values.

## Compatibility

Protocol version is currently **1.0**, independently of the BookText release.
Consumers must reject an unknown major version and may ignore unknown event
fields and event types in a compatible minor version. In machine mode stdout
contains JSONL protocol records only; delegated human-Core diagnostics go to
stderr.

## Commands

```text
booktext machine preflight [--operation run|export|cdm|final-build|review|translation]
booktext machine run SOURCE... -- [existing BookText run options]
booktext machine inspect OUTPUT_DIR
booktext machine inspect OUTPUT_DIR --record-activity
booktext machine project-history [--known-output OUTPUT_DIR ...]
booktext machine project-remove PROJECT_ID
booktext machine review OUTPUT_DIR [--page N]
booktext machine decision OUTPUT_DIR --item ID --action ACTION [...]
booktext machine final-build OUTPUT_DIR
booktext machine cdm OUTPUT_DIR
booktext machine translation OUTPUT_DIR --source-language en --target-language ru --model MODEL [--keep-alive 30s]
booktext machine translation-validate OUTPUT_DIR --source-language en --target-language ru
booktext machine export OUTPUT_DIR --format FORMAT [--format FORMAT ...]
```

The command layer calls existing production Core functions for processing,
review decisions, final build, CDM build, and publication export. It does not
duplicate their business logic.

## Durable project history

`project-history` is a Core-owned, versioned local index at
`~/Library/Application Support/BookText/project-history.json` (or the explicit
`BOOKTEXT_HISTORY_PATH` used by an integrator). It contains only stable job
identity, output location, source reference, display name, and activity times.
It is **not** a second workflow state store: every list/relaunch reconciles a
row through `inspect`, without OCR, model use, or artifact regeneration.

The resulting `project` projection has one of `READY_TO_PROCESS`,
`PROCESSING`, `INTERRUPTED_RESUMABLE`, `REVIEW_REQUIRED`,
`READY_FOR_FINAL_BUILD`, `BOOK_READY`, `TRANSLATING`, `TRANSLATION_READY`, or
`FAILED`, together with a source/translation availability state and a stable
action label. A moved source is reported as `SOURCE_UNAVAILABLE`, but a valid
existing output remains reopenable for review, final-build, translation, or
export; only a resume that needs the source is blocked. Missing project files
are reported as `PROJECT_FILES_UNAVAILABLE`. `project-remove` removes only an
index row and never deletes source or BookText artifacts.

For a dataless iCloud-managed output, history intentionally returns the same
controlled unavailable state without probing artifacts, writing a job file, or
requesting download/materialization. A user can make a local copy available
and then reopen it; the background recent-project refresh never waits on cloud
I/O.

`export` requires at least one format: `markdown`, `html`, `docx`, `epub`,
`pdf`, or `pdf_image`. `all` is supported only as an explicit format choice;
Core does not default an omitted selection to every format.

Translation defaults to a bounded `--keep-alive 30s` request lease for its
explicit local Ollama model. This allows sequential blocks to reuse the model,
then lets Ollama release it naturally without BookText stopping the shared
daemon or affecting another client's process.

## JSONL events and exits

Every record has `protocol_version`, `job_id`, `event_type`, `phase`,
`current`, `total`, `page`, `code`, `message`, and `details`. Stable event
types are: `job_started`, `phase_started`, `progress`, `warning`,
`artifact_created`, `review_summary`, `phase_completed`, `job_completed`,
`job_failed`, and `job_cancelled`. Stable UI phases are `preflight`, `prepare`,
`ocr`, `qa`, `visuals/tables`, `review`, `final_build`, `cdm`, `translation`, and `export`.

Exit codes: 0 success; 2 invalid input; 3 preflight/dependency failure; 4
processing failure; 5 validation failure; 6 stale/conflict; 130 cancellation.

Each output directory contains `.booktext_job.json`: durable job ID, status,
last phase, timestamps, and logical artifact map. `inspect` recovers state
without OCR. The artifact map exposes logical keys rather than requiring the
app to hard-code filenames.

For a running `machine run`, the app cancels by sending SIGINT or SIGTERM to
the launched Core process. The wrapper terminates only its owned child session
with the existing lifecycle mechanism, persists `cancelled`/partial state, and
emits `job_cancelled`. No global process-name kill is used.

Contract examples for a Swift client are in `tests/fixtures/machine_protocol/`.
