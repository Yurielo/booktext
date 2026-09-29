# Resumable chunk processing

Large BookText OCR runs use a persisted execution plan at
`chunks/chunk_plan.json`.  This is an operational boundary, not a semantic
boundary: canonical text, CDM blocks, review identifiers, and source-page order
remain unchanged.

## Bounded execution

`AUTO` plans normally own eight pages per Surya OCR chunk (one chunk for a
small document).  A caller can select a safe explicit value:

```bash
booktext source.pdf --chunk-size 5
```

The plan records the source fingerprint, operation/options fingerprint, owned
pages, read-only adjacent context pages, attempts, checkpoint manifests and
validation hashes.  A changed source/options fingerprint fails closed as
`stale`; it is never silently mixed with old raw OCR evidence.

Heavy chunks execute sequentially:

`process → validate → atomic checkpoint → verify → release transient input → next chunk`.

Completed valid chunks resume without OCR.  A corrupt checkpoint is invalidated
individually; neighbouring valid chunk checkpoints are retained.  Context pages
are recorded only for future context-sensitive operations (such as translation)
and are never appended to canonical output by a second chunk.

## Disk and cleanup lifecycle

Each artifact has one lifecycle role:

- `transient`: staged images, model payloads and scratch files;
- `checkpoint`: validated results required to resume;
- `evidence`: raw OCR/provenance/review material;
- `canonical`: final book, CDM, decisions and exports.

BookText keeps an authenticated marker inside its managed `chunks/` workspace.
Cleanup validates the marker and path containment, retains compact manifests and
canonical raw Surya evidence, and deletes only regenerable checkpoint payloads
after final assembly has passed validation.  Cleanup is idempotent.  It never
searches `/tmp`, follows a symlink, or deletes a path outside the current
BookText output directory.

The machine operation is:

```bash
booktext machine cleanup /path/to/output
```

It returns JSONL lifecycle events and a structured result with
`bytes_before`, `bytes_deleted`, `bytes_remaining`, `files_deleted` and retained
categories.  The native **Clean Temporary Files** action invokes this Core
operation; Swift does not decide what is safe to delete.

## Machine protocol

Protocol 1.0 additionally emits backward-compatible chunk lifecycle events:
`chunk_plan_created`, `chunk_started`, `chunk_progress`, `chunk_completed`,
`chunk_failed`, `chunk_retry`, `assembly_completed`, and `cleanup_completed`.
Existing clients that ignore unfamiliar events remain valid.

Chunking bounds a continuous inference operation and allows resource release;
it does **not** promise a lower instantaneous CPU/GPU load.
