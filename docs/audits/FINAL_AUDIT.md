# BookText final autonomous release audit v3

Audited code baseline: `fa98c3c1570d7c9bd3f463d7f09df66c0f90696e`  
Audit date: 2026-09-30  
Scope: local macOS BookText baseline; no new product phase, OCR benchmark, or translation work.

## Result

**PASS — no open P0/P1 release blockers.**

The audit covered a signed native app build, a normal seven-page document
workflow, a difficult mixed visual/text PDF, and a resumable 28-page OCR run.
The audit found two blocking production defects.  Both were fixed and merged
before the final run:

- #106 / PR #107: a valid figure-only page returned by Surya no longer aborts
  an otherwise valid document.  It is represented as an empty OCR page without
  inventing text.
- #108 / PR #109: cleanup now records the exact descendant PID lineage of a
  BookText-owned Surya process, including a `llama-server` that starts its own
  session.  Cleanup never uses name-based termination.

## Environment and baseline

- BookText: `0.1.0`
- Machine protocol: `1.0`
- Host: macOS 26.5.2, arm64
- Regression suite: `python3 -m unittest discover -s tests -v` — **150 PASS**
- Native app: `./scripts/build_booktext_app.sh` — PASS
- Native protocol tests built by the app script — PASS
- `codesign --verify --deep --strict --verbose=4 BookTextApp/build/BookText.app`
  — PASS
- Normal LaunchServices launch (`open -n`) — PASS

## Gate 1 — build and regression

The current baseline built successfully, its bundle was signed only after
assembly, strict code-sign verification passed, and the entire relevant Python
and Swift-protocol regression suite passed.

## Gate 2 — normal workflow

The existing real seven-page *Universal Declaration of human rights* output
was copied into the isolated audit workspace and processed offline through the
production final-build, CDM, and export operations.  It retained 7 ordered
pages and a 87-block CDM.  All five requested exports were present, non-empty,
and validated:

| Format | Bytes |
| --- | ---: |
| Markdown | 22,695 |
| HTML | 25,486 |
| DOCX | 17,514 |
| EPUB | 11,713 |
| PDF | 56,623 |

The export manifest lists exactly those five formats.  The accepted native
reader preview is a reader representation, not the CDM diagnostics view.

## Gate 3 — difficult visual/text document

Real input: `Franz Bardon - The Practice of Magical Evocation-483-484.pdf`
(2 pages, unencrypted).  The first page is figure-only; the second contains
extractable text.  The initial run exposed #106.  After PR #107, the document
completed with both pages accounted for, one empty figure-only OCR result,
raw OCR preserved, visual regions represented as evidence, and no fabricated
text or table cells.

Real review-decision exercise used the production machine protocol:

- `keep_surya`;
- valid `accept_vision`;
- a source-anchored manual replacement;
- final build and CDM rebuild.

The review decision tests additionally prove stale/conflict fail-closed
behavior and that table fallback stays review evidence rather than verified
cells.

## Gate 4 — 28-page resume and cleanup

Real input: `O_V_Allakhverdova_-_Mediatsia_-_novaya_kommunikativnaya_praktika_razreshenii_konfliktov.pdf`
(28 pages, unencrypted).

- Automatic plan: four ordered chunks: `1–8`, `9–16`, `17–24`, `25–28`.
- A controlled interruption occurred after chunk `1–8` had been atomically
  checkpointed.  No BookText-owned Surya or `llama-server` descendants
  remained afterward.
- Resume reused the 28 prepared source pages and completed only the unfinished
  chunks.  The final plan marks every chunk `complete`; chunk `0002` records
  the prior interrupted attempts rather than silently discarding that history.
- Final output: **28 processed / 0 failed**, 28 unique ordered raw Surya page
  keys, 28 unique ordered pages in `book.json`, and a 211-block CDM.
- Safe compaction removed completed per-chunk `result.json` checkpoints only
  after verified canonical raw assembly.  The compacted plan remains as
  provenance; no source evidence was deleted.
- Final-build, CDM, and inspect machine operations all emitted
  `job_completed` without rerunning OCR.

The real run also encountered one page with no Surya text.  It completed as a
valid empty result, preserving page identity and avoiding invented content.

## Lifecycle safety

The controlled interruption and the final normal-completion run both left zero
BookText-owned `surya_ocr` / `llama-server` descendants.  The audit did not
terminate any process by name and did not touch unrelated user or system
processes.  Regression coverage includes normal completion, failure, forced
cleanup, SIGINT/SIGTERM, idempotence, concurrent registry isolation, and an
independent descendant that creates a new session.

## Non-blocking notes

- The long gate deliberately used `--no-qa` to focus this gate on resumable
  OCR and lifecycle behavior; it is not evidence that QA was skipped in the
  normal workflow gate.
- An unauthenticated Hugging Face warning was emitted by Surya's environment;
  local cached model execution completed successfully and this did not affect
  correctness or lifecycle cleanup.

## Release decision

The audited BookText baseline is ready for local use.  No P0/P1 blocker remains
from the four audit gates.  Post-audit local artifact inventory and only
ownership-safe cleanup are tracked separately in Issue #104.
