# Issue 131 TranslateGemma replacement evidence

Date: 2026-10-02. Baseline `origin/main`: `a51bded50469fd0088d50fcf8af70e4e2bb6b17e`.
Owner chose TranslateGemma Variant A over Qwen Variant B for all five EN→RU comparison excerpts. The comparison provenance and Word report remain under `Documents/BookTextAcceptance/model-comparison-en-ru/` on the owner's Mac. On 2026-10-03, after reviewing the compact production comparison report, the owner stated: “перевод хороший. human gate passed”. This accepts the changed production wording, the flagged numeric-review sample, and the separate RU→EN sample for this release.

## Model and contract

- Local Ollama tag: `translategemma:4b`.
- Installed digest: `c49d986b0764f5881c476eb21435bb62b7abc62347aab3d4a6071e811be510a1`.
- Prompt version: `translategemma-document-translation-v1`; one user message, explicit language names/codes, plain-text translation only.
- Official prompt: https://ollama.com/library/translategemma . Installed `ollama show translategemma:4b --modelfile` confirmed the local user-turn template.
- Qwen inference is denied by Core before provider access. The three `tools/hybrid_*` Qwen experiments are archival OCR audit scripts and have no active production, app, pipeline, bootstrap or acceptance references.

## Bounded production pilots

All runs used copied `issue-116-pipeline` artifacts through `booktext.py machine`; no OCR or full-book translation ran. Full local request inputs, response text, settings and attempt records are in `work/pilots/five-evidence.json`; machine JSONL logs are in `work/pilots/`.

| Pilot | Result |
| --- | --- |
| Five accepted EN→RU IDs | All five owned units assembled in CDM order; four `machine_translated`, one `needs_numeric_review` (`block-2b605df75fc720425eb6`); no missing or duplicate IDs. Production wording differs from accepted Variant A. |
| RU→EN | One saved Russian Variant A excerpt translated through production Core; source block `block-5c70e9450e8c0397cfbf`; structural and numeric validation PASS; owner semantic acceptance recorded above. |
| Three adjacent units | Interrupted after verified chunk 1 by sending SIGINT only to the BookText child process; resumed machine invocation reused chunk 1 and translated chunks 2–3. Final coverage and ordering PASS. |
| Legacy migration | Copied Qwen manifest and checkpoint workspace archived under content-addressed history; new manifest identifies TranslateGemma. Historical Qwen revision exported offline via `--history-sha` without inference. |
| DOCX export | Existing translation exporter produced a valid DOCX, retaining two CDM figures. |
| Resource lease | Pilot with requested `5s`, effective `30s`, timeout `10s`: TranslateGemma present immediately after completion, absent after lease expiry. No process-name termination or daemon stop was used. |

Original and pilot SHA-256 hashes match for `book_document.json` (`92d5007156d0e8c8c7be8b00a1d7cb36a4bb72e680f2a16dde612996b9c8cd84`), `book.json`, `review.json`, `review_priority.json`, `review_decisions.json`, `run_summary.json`, and all four image files. Translation export reports `figure_count: 2` and DOCX format validation PASS.

## Tests and review gate

- Full Python production suite: 237 tests, PASS.
- Swift protocol executable: `BookTextProtocolTests: PASS`.
- Swift release build: PASS.
- Signed review bundle at `/private/tmp/BookText-issue-131-review.app`: `codesign --verify --deep --strict --verbose=4` PASS; LaunchServices launch confirmed. This is a review build, not an installed release.
- `git diff --check`: PASS.

The human semantic gate passed on 2026-10-03. Merge, installed Core and app update, and post-merge verification remain the release steps.
