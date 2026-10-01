# Automatic document extraction

The native app exposes one extraction workflow. `--ocr auto` is the CLI default;
explicit `--ocr surya`, `--ocr tesseract` and `--native-text-only` remain legacy
compatibility options, not competing app modes. Auto outputs use `_book_auto`,
or an explicit `--output-dir`. Existing `_book` results, decisions and translations
are never implicitly migrated or overwritten.

## Routing and ownership

For PDF, Poppler's word boxes provide native text and flow/line geometry. Invalid
characters and duplicate text-layer words reject that text layer. MuPDF drawing
transforms inventory raster and vector regions. Native text is authoritative
where usable. Regular residual text-row evidence routes raster text to the existing
primary Surya pipeline; native words and visual regions are masked first. Vision
compares only the OCR portion; it never replaces native or Surya text automatically.
DjVu/images continue through the existing Surya/exclusion pipeline.

For figures inside a raster scan, retained primary Surya layout boxes supply
source crops. Their raw blocks stay intact as evidence; figure-labelled text is
not promoted to body text/translation, and those boxes are masked for Vision QA.
No second recognition or generated figure description is requested.

Native-only PDF runs need Poppler, MuPDF and Pillow, not Surya, Vision, Tesseract
or Ollama. OCR dependencies are checked only when analysis finds OCR work.
The app no longer silently supplies `--raw-ocr`, `--no-qa` or Tesseract.
Searchable PDF creation is opt-in in auto (`--searchable-pdf`); this optional
operation can require Tesseract/OCRmyPDF and respects existing exclusions.

Images are decoded from embedded assets only when object occurrence, dimensions
and an orthogonal, unclipped display transform match exactly. Otherwise the
rendered source region is retained. Display orientation, source asset/hash,
page/box, source fingerprint and no-OCR/no-translation provenance are recorded.
Only figure crops, never a full-page surrogate for an extracted illustration,
enter CDM/export. Paragraph text overlapping an embedded image's blank margin
is not baked into the independent image asset. Crop DPI retains physical size;
Small DOCX figures wrap text without overlap; large illustrations stay inline,
clear earlier floats and fit within the printable page. Captions are not invented.

Scan/figure discrimination is conservative, not a semantic oracle. Ambiguous
rasters remain images with `ambiguous_raster_preserved` warnings. Native tables
without proven cell structure remain text/visual evidence, not verified cells.
Vector groups remain unverified source crops. Short scans, unusual layouts,
masked/composite images and damaged PDF image streams need visual review.
Render diagnostics are retained, not repaired by fabricated graphics. A usable
text layer is not a guarantee that the source publisher's text was correct.

## Persistence and provenance

`extraction/source.json` anchors source bytes, selected pages and processing
options. `native.xml`, per-page trace and render diagnostics retain evidence.
`inventory.json` accounts for every source page, including unselected pages;
`report.json` reports actual routing. `artifacts.json` checkpoints source evidence
hashes after each prepared page. Resume validates those hashes before reuse;
changed source/options/artifacts fail closed. OCR uses the existing resumable
chunk executor. Temporary masks stay in the run's `.work` directory. All new
external commands use the owned-process registry; Vision QA now also uses it,
with a 120-second timeout and cleanup in `finally`.

CDM validates saved extraction evidence and reads native geometry alongside
Surya layout. Figures are inserted at their geometric location, preserving
native column flow. Valid text-changing decisions retain the existing safe
page-text projection (fine layout can be uncertain after manual correction).

## Reversible publication text

Raw page text and QA remain unchanged. CDM applies layout normalization **after**
valid human decisions. `normalization` records its version, each original text,
result hash and reasons. Single physical line breaks in evidenced prose become
spaces. Lists, numeric columns, tables and short verse are protected. Letter/comma/
letter spacing does not touch numeric commas. No glued word is guessed apart.

A line-end or cross-page hyphen is removed only when the unhyphenated whole word
occurs at least twice in the source and the competing compound is not attested.
Otherwise the hyphen is retained. Cross-page joins require adjacent paragraph
blocks on consecutive source pages; the continuation retains its block ID,
consumed-by link and reversible original text. This avoids translating a fragment
such as `dance` independently from `accor-`. Rebuild is deterministic; changed CDM
fingerprints correctly make old translation checkpoints stale, not silently reusable.

Existing translation checkpoints and the bounded model lease remain unchanged.
Validation adds review-only signals for numeric changes, mixed scripts inside a
word, possibly untranslated long prose and unresolved terminal hyphens. These
signals are not automatic correction or proof of mistranslation; proper names,
quotations and unusual identifiers can legitimately trigger review. Validation
rechecks existing translations offline without rewriting their source evidence.

## Verification

Run `python3 -m unittest discover -s tests -v` and
`swift run --package-path BookTextApp BookTextProtocolTests`.
`tests/test_automatic_extraction.py` includes the real CLI/machine path, geometry,
partial text layers, ownership cleanup, stale evidence, reversible paragraph and
boundary handling, and translation quality evidence. Real-book evidence belongs
in Issue #117; a full OCR or translation rerun is not required to audit native
PDF inventory and targeted assembly.
