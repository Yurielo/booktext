# Booktext → Integra OCR adapter contract, draft v0.1

Status: `DRAFT`. This document specifies the adapter boundary to be implemented
in BT-07. It does not claim that a Booktext result is already imported into
Integra Knowledge.

## Purpose

Integra's Document and OCR Pipeline accepts a local OCR provider result with a
provider identity and complete, normalized OCR blocks. Booktext will emit an
adapter candidate that has the same `provider` and `blocks` representation,
plus enough source metadata to verify that the candidate belongs to the exact
source revision.

The adapter does not create Topics, Concepts, Relations, Evidence claims, or
ReviewDecisions. Integra owns the later Knowledge workflow. Booktext supplies
machine-derived document evidence with explicit status and provenance.

## Candidate format

```json
{
  "schema_version": "0.1",
  "artifact_type": "booktext.integra_ocr_candidate",
  "source": {
    "sha256": "<64 lowercase hexadecimal characters>",
    "format": "IMAGE",
    "page_count": 1,
    "revision_id": "booktext-source-revision-..."
  },
  "provider": {
    "component_id": "booktext.surya",
    "implementation_sha256": "<64 lowercase hexadecimal characters>",
    "model_id": "surya-ocr-2-gguf",
    "model_sha256": "<64 lowercase hexadecimal characters>"
  },
  "blocks": [
    {
      "unit_kind": "IMAGE",
      "unit_index": 1,
      "region": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
      "text": "Visible text from this region.",
      "confidence": 0.97
    }
  ]
}
```

The `provider` and `blocks` values are passed unchanged to the current Integra
OCR callback validator. The remaining top-level fields remain Booktext adapter
metadata and must not be injected into Integra's closed OCR callback object.

## Required mapping

| Booktext source | Adapter field | Rule |
| --- | --- | --- |
| Original bytes | `source.sha256` | SHA-256 of the original input, never a preprocessed copy. |
| Input type | `source.format` | `IMAGE`, `PDF`, `DOCX`, or `PPTX`; DjVu needs a separate accepted path because the present Integra contract has no DjVu format. |
| Original page/image order | `source.page_count`, `unit_index` | One-based order; all units requiring OCR must be covered. |
| Surya block polygon/bbox | `region` | Convert to normalized `{x, y, width, height}` in original image coordinates. Coordinate system and orientation conversion must be recorded during BT-03. |
| Chosen block text | `text` | Non-empty verbatim machine text. Do not replace with a language-model correction. |
| Engine confidence | `confidence` | A numeric engine confidence only when its documented meaning matches the emitted block. No fabricated confidence. |
| Engine/runtime build | `provider.implementation_sha256` | SHA-256 of the actual adapter/engine implementation used for this revision. |
| Exact OCR model bytes | `provider.model_sha256` | Required by Integra. If the model hash cannot be obtained, the adapter must fail rather than invent one. |

## Assurance and review states

`status` belongs in the Booktext manifest and review package, not in the
closed Integra OCR block shape. At minimum the adapter records:

- `COMPLETE_MACHINE_OCR` — every required page has blocks and valid provider
  provenance;
- `PARTIAL_MACHINE_OCR` — must not be submitted to the current Integra
  validator because it requires complete coverage;
- `REVIEW_REQUIRED` — OCR is complete but has recorded disagreements,
  uncertainty, truncation, or requested human review;
- `HUMAN_VERIFIED` — only after an explicit Booktext verification record.

Agreement between Surya and Vision is evidence for review priority; it does not
create `HUMAN_VERIFIED` or semantic acceptance.

## Relationship to Knowledge

After Integra accepts the document OCR result, its document pipeline can retain
page/image and region locators. A later Knowledge operation must bind every
substantive evidence reference to that retained document evidence. It must not
invent word/time identifiers for a page-based document, and it must retain the
machine/human assurance state.

The `tools/validate_integra_ocr_contract.py` probe validates a candidate's
`provider` and `blocks` against an installed Integra source tree. It proves
contract compatibility only; it is not a Knowledge import and makes no writes
to Integra.

## Deliberate exclusions for v0.1

- No direct semantic-authoring format.
- No acceptance, alias merge, canonical Knowledge mutation, or source rewrite.
- No silent fallback when a model hash, page coverage, confidence, or region is
  unavailable.
- No DjVu ingestion claim until Integra exposes an accepted mapping.
