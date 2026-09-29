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
booktext machine review OUTPUT_DIR [--page N]
booktext machine decision OUTPUT_DIR --item ID --action ACTION [...]
booktext machine final-build OUTPUT_DIR
booktext machine cdm OUTPUT_DIR
booktext machine translation OUTPUT_DIR --source-language en --target-language ru --model MODEL
booktext machine translation-validate OUTPUT_DIR --source-language en --target-language ru
booktext machine export OUTPUT_DIR --format FORMAT
```

The command layer calls existing production Core functions for processing,
review decisions, final build, CDM build, and publication export. It does not
duplicate their business logic.

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
