# Issue 116 sequential pipeline completion

Date: 2026-10-03. Starting main: `8757235d9ffcf8ba0b6380656f8e601859c12233`.

PR #130 had already installed Core-owned linear pipeline planning, stage persistence,
Project History and native configuration. Its real Qwen translation was rejected
on semantic grounds. PR #132 subsequently replaced active translation with the
owner-accepted `translategemma:4b`. This completion work retains that contract.

## Correctness fixes

- Processing always writes to the pipeline's project root, including when an
  explicit output directory is selected. The source and configured directory
  must match an existing project before it can be reused.
- Completed stage references include file hashes. Resume checks source,
  final-build inputs, CDM schema/provenance, production translation validation,
  and each requested export's manifest and file hash. If an artifact fails,
  only it and dependent stages are invalidated. A per-project worker lock
  rejects concurrent starts without changing the active job state.
- Translation validation is read again from the persisted TranslateGemma
  artifact. Numeric or quality signals pause before export. No Qwen fallback or
  translation prompt change was made. The existing explicit Qwen migration
  operation is retained.
- Export format and ORIGINAL/TRANSLATION target are validated before Start.
  Translation export must reference the selected pair's manifest; the output
  manifest and generated files are checked before EXPORT is marked complete.

## Real primary acceptance

Source: four unmodified pages extracted from the owner's existing
`Brain flushing_compressed.pdf`. The PDF contains genuine embedded English text
and retained figures. This small local excerpt was processed with the existing
native PDF text path; no 333-page corpus was used. Pilot files and JSONL logs
are under `work/issue-116/primary-clean/` in the task worktree (local, ignored
from Git).

Before Start, Core saved PROCESS → SKIP HUMAN REVIEW → FINAL BUILD → CDM →
TRANSLATE EN→RU → EXPORT translated DOCX. One `pipeline-start` command produced
stage_started and stage_completed for all six stages in that order, then
pipeline_completed. No stage-transition command was sent. `book_document.json`
and the validated `translations/en-ru.json` were produced. Its provider is
Ollama `translategemma:4b`, digest
`c49d986b0764f5881c476eb21435bb62b7abc62347aab3d4a6071e811be510a1`.
All seven logical units are `machine_translated`; numeric and quality review
counts are zero. The final `translations/exports/en-ru/book-en-ru.docx` is a
valid 712,033-byte DOCX. Its manifest is a translation export, names
`translations/en-ru.json`, matches the source CDM hash and reports DOCX PASS.
The DOCX XML contains Russian text from the translation; the English CDM was
not exported in its place. It rendered to six pages for visual inspection.

## Real interruption and resume

A separate run used the same real source with two-unit translation chunks. The
pipeline was interrupted with SIGINT to its own worker after chunk 1 was
complete and one unit of chunk 2 had an atomic partial checkpoint. The worker
exited 130; persisted state was PAUSED at
TRANSLATE, with PROCESS, REVIEW POLICY, FINAL BUILD and CDM complete. Project
History and inspect both reopened that project as `PIPELINE_PAUSED` at
`translate` with a Resume action. `pipeline-resume` emitted
`resumed` for chunk 1's two verified units and `partial_resume` for the first
unit of chunk 2, started only TRANSLATE and EXPORT, and completed the same
translated DOCX. No upstream stage or saved translation unit was
repeated. A subsequent resume of the completed primary run started zero stages.

The native app's Stop path sends a signal to the same Core worker. During the
real interruption, the worker preserved its translation checkpoint and did not
start EXPORT. After both completions, no BookText-owned Python/OCR or inference
child remained. The external Ollama daemon and its bounded model lease were
left intact.

## Automated checks

Focused tests cover invalid plans, source/project collision, explicit legacy
Qwen migration, review pause, quality-signal pause, automatic transitions,
translated export routing, stale DOCX retry without repeating OCR/translation,
concurrent-start rejection, and machine projection. The full production Python
suite and Swift protocol/release build are release gates; their final results
are recorded on the PR and Issue after the final commit.
