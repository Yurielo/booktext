# Automatic document extraction

The native app exposes one extraction workflow. `--ocr auto` is the CLI default;
explicit `--ocr surya`, `--ocr tesseract` and `--native-text-only` remain legacy
compatibility options, not competing app modes. Auto outputs use `_book_auto`,
or an explicit `--output-dir`. Existing `_book` results, decisions and translations
are never implicitly migrated or overwritten.

## Routing and ownership

Policy `automatic-extraction-2` also distinguishes a page scan with a preserved
invisible OCR layer from visible digital text. MuPDF `ignore_text` glyph origins
must align with at least 20 native words inside a raster covering 80% of the
page. Those invisible words are evidence only: the visible scan is rendered at
300 dpi and sent to primary Surya. Visible native words remain eligible for
direct extraction and are masked only in the OCR input. A hidden watermark or
hidden text without a page scan does not trigger this route.

The raw text layer remains in `extraction/native.xml`; rendering provenance and
route reasons remain in `extraction/report.json`. No lexical heuristic claims
that one text is correct. Apple Vision retains its existing independent QA role.
A whole-page scan is not exported as a figure simply because it is a raster.
Surya layout, structured tables, fallback review and figure crops use the same
production pipeline as scanned PDFs without a text layer. Old-policy workspaces
fail closed on resume; select a new output directory to reprocess under v2.

Dense scan OCR reserves 8192 input-image/prompt tokens in each llama.cpp context
slot in addition to Surya's unchanged 12288 generation-token budget. An explicit
total context override that cannot fit the per-slot budget fails closed rather
than silently truncating a page. Larger explicit per-slot budgets are retained.
The default is two inference slots to bound reserved KV memory; explicit
`SURYA_INFERENCE_PARALLEL` settings are preserved.
These budgets are part of the persisted chunk options fingerprint: changing
them cannot reuse an older potentially truncated checkpoint silently.

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

A discretionary soft hyphen followed by a physical line break is explicit source
evidence. For a hard line-end or cross-page hyphen, one independent unbroken
occurrence of the whole word is sufficient, provided the competing compound is
not attested. Otherwise the hard hyphen is retained, not guessed from a dictionary
or a suffix. `normalization.review_candidates` records ambiguous within-page
hyphens with block/page identity, original span/text/hash and a warning visible
beside the paragraph in Native Preview. A unique `biomagnetical-ly` therefore
still requires human judgment; geometry alone cannot prove a spelling.

Saved native first-line indents and gaps reconstruct paragraph boundaries offline.
Ordinary cross-page continuation requires consecutive source pages, adjacent prose
blocks, compatible wide body geometry at the page bottom/top, a full last line,
an unindented lowercase continuation and no sentence-final punctuation. Headings,
figures, tables, narrow columns, lists and absent geometry prevent this join.
Continuations retain every block ID, source page and consumed-by link; the log
reverses chains across multiple pages. No join crosses an omitted page.

Lowercase `i. e.` / `e. g.` spacing is normalized only in Latin prose with a
lowercase following word (or paragraph end). Initials, numbers, scientific
hyphens, paths and structural blocks are not globally rewritten. These typography
changes are versioned/reversible, not semantic corrections.

`selection` validates actual pages against stored requested pages and source
total (or saved legacy include/exclude evidence). Missing, duplicate, unexpected
or out-of-order pages fail closed. Legacy outputs without a known selection remain
explicitly unverifiable, not falsely complete. Intentional omissions are allowed:
their `page_break` blocks carry `boundary_kind: excerpt_gap`, exact omitted range
and a reader-facing notice in native Preview, Markdown, HTML, DOCX, EPUB and PDF.
Ordinary consecutive page boundaries remain provenance, not artificial rules or
paragraph breaks. Acceptance samples must use contiguous ranges; separate excerpts
must be visibly separated.
Partial selections also carry a title-level notice listing selected source ranges;
the first/last excerpt may start/end within a paragraph, not a complete chapter.

Rebuild is deterministic; changed CDM fingerprints correctly make old translation
checkpoints stale, not silently reusable. Use `booktext document build OUTPUT`
(or `booktext machine cdm OUTPUT`) then export to regenerate publication output
from existing evidence without OCR. Previously exported files do not change merely
because the application was updated; preserve earlier outputs before rebuilding.

Existing translation checkpoints and the bounded model lease remain unchanged.
Validation adds review-only signals for numeric changes, mixed scripts inside a
word, possibly untranslated long prose and unresolved terminal hyphens. These
signals are not automatic correction or proof of mistranslation; proper names,
quotations and unusual identifiers can legitimately trigger review. Validation
rechecks existing translations offline without rewriting their source evidence.

## Verification

For scans with no visible native-text blocks, CDM keeps the primary Surya
reading order rather than interleaving columns by vertical position. Retained
figure crops with matching raw-layout provenance and bounds occupy that same
primary position exactly once; unanchored assets are not assigned a guessed
semantic position.

Run `python3 -m unittest discover -s tests -v` and
`swift run --package-path BookTextApp BookTextProtocolTests`.
`tests/test_automatic_extraction.py` includes the real CLI/machine path, geometry,
partial text layers, ownership cleanup, stale evidence, reversible paragraph and
boundary handling, and translation quality evidence. Real-book evidence belongs
in Issue #117; a full OCR or translation rerun is not required to audit native
PDF inventory and targeted assembly.
