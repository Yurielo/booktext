"""Reversible publication whitespace projection, not OCR/semantic correction."""
import hashlib
import re
import statistics
from collections import Counter

VERSION = "1.1"
LETTERS = r"A-Za-zА-Яа-яЁё"


def vocabulary(texts):
    return Counter(word.casefold() for text in texts for word in re.findall(rf"[{LETTERS}]+(?:-[{LETTERS}]+)*", text))


def normalize(text, *, kind, trusted_lines=False, words=None):
    words = words or {}
    if kind not in {"paragraph", "heading", "caption"}:
        return text, []
    lines = text.splitlines()
    # Lists, columns, verse and numeric rows are not prose wrapping evidence.
    protected = any(re.match(r"\s*(?:[-•*]|\d+[.)])\s", line) or "\t" in line
                    or re.search(r"\S  {2,}\S", line)
                    or re.fullmatch(r"[\d\s.,/+−%°():-]+", line) for line in lines)
    prose = trusted_lines or (len(lines) > 1 and sum(len(line) >= 40 for line in lines[:-1]) >= len(lines[:-1])*.8)
    if protected or not prose:
        return text, []
    value = text
    signals = []
    def join(match):
        left, right = match.group(1), match.group(2)
        whole, compound = left+right, left+"-"+right
        # One independent, unbroken source occurrence is evidence. Requiring
        # two needlessly leaves rare words broken; spelling guesses are not.
        if words.get(whole.casefold(), 0) >= 1 and not words.get(compound.casefold(), 0):
            signals.append("corpus_attested_line_hyphen_join")
            return whole
        signals.append("preserved_ambiguous_hyphen")
        return compound
    value = re.sub(rf"([{LETTERS}]+)-\n[^\S\n]*([a-zа-яё][{LETTERS}]*)", join, value)
    # A discretionary hyphen is explicit source evidence. Never cross a
    # blank paragraph or eat a list/structural boundary with \s*.
    value = re.sub(rf"(?<=[{LETTERS}])\u00ad\n[^\S\n]*(?=[{LETTERS}])", "", value)
    value = re.sub(r"(?<!\n)\n(?!\n)", " ", value)
    # Letter/comma/letter only. Numeric commas, fractions and identifiers remain.
    value = re.sub(rf"(?<=[{LETTERS}]),(?=[{LETTERS}])", ", ", value)
    # Narrow typography only, in Latin prose. Not arbitrary period spacing,
    # initials, numbers, paths, or identifiers; uppercase initials stay intact.
    if kind == "paragraph" and len(re.findall(r"[A-Za-z]", value)) >= 20 and not re.search(r"[А-Яа-яЁё]", value):
        value, count = re.subn(r"(?<![\w./\\`])(?:i\. +e\.|e\. +g\.)(?= +[a-z]|$)",
                              lambda m: m[0].replace(" ", ""), value)
        if count:
            signals.append("latin_abbreviation_spacing")
    if value != text:
        signals.append("prose_layout_whitespace")
    return value, sorted(set(signals))


def line_geometry(lines, page_size, bounds):
    """Compact native layout evidence; no language or semantic inference."""
    if not lines or not page_size or not bounds:
        return None
    boxes = [line.get("bbox") for line in lines]
    if any(not isinstance(box, list) or len(box) != 4 for box in boxes):
        return None
    height = statistics.median(box[3]-box[1] for box in boxes)
    if height <= 0 or bounds[2] <= bounds[0]:
        return None
    return {"source": "native_word_lines", "page_size": list(page_size), "bbox": bounds,
            "first_line": boxes[0], "last_line": boxes[-1], "line_height": height,
            "long_line_ratio": sum(len(line.get("text", "")) >= 40 for line in lines)/len(lines)}


def _continuous_prose(left, right):
    a = left.get("provenance", {}).get("line_geometry")
    b = right.get("provenance", {}).get("line_geometry")
    if not a or not b or a.get("source") != "native_word_lines" or b.get("source") != "native_word_lines":
        return False
    lh, rh = a["line_height"], b["line_height"]
    aw, bw = a["bbox"][2]-a["bbox"][0], b["bbox"][2]-b["bbox"][0]
    # Require wide, similarly sized body columns at bottom/top of consecutive
    # pages, a full last line and an unindented continuation. Typography alone
    # never joins sentence-final text, headings, verse, lists or numeric rows.
    return (min(a["long_line_ratio"], b["long_line_ratio"]) >= .8
            and abs(lh-rh) <= min(lh,rh)*.15
            and abs(aw-bw) <= min(aw,bw)*.08
            and aw >= a["page_size"][0]*.55 and bw >= b["page_size"][0]*.55
            and a["last_line"][3] >= a["page_size"][1]*.8
            and b["first_line"][1] <= b["page_size"][1]*.15
            and a["bbox"][2]-a["last_line"][2] <= lh*1.5
            and abs(b["first_line"][0]-b["bbox"][0]) <= rh*.5
            and bool(re.search(rf"[{LETTERS},]$", left.get("text", "")))
            and bool(re.match(r"[a-zа-яё]", right.get("text", "")))
            and not any(re.search(r"(?:^|\n)\s*(?:[-•*]|\d+[.)])\s|\t", x.get("text", "")) for x in (left,right)))


def project(blocks, source_texts):
    words = vocabulary(source_texts)
    transforms, candidates = [], []
    for block in blocks:
        before = block.get("text", "")
        after, reasons = normalize(before, kind=block["type"], trusted_lines=block.get("provenance", {}).get("layout_label") == "Text", words=words)
        if "preserved_ambiguous_hyphen" in reasons:
            for match in re.finditer(rf"([{LETTERS}]+)-\n[^\S\n]*([a-zа-яё][{LETTERS}]*)", before):
                whole, compound = match[1]+match[2], match[1]+"-"+match[2]
                if words.get(whole.casefold(), 0) and not words.get(compound.casefold(), 0):
                    continue
                candidate = {"kind": "ambiguous_line_hyphen", "block_id": block["block_id"],
                             "page": block["provenance"]["source_page"], "source_span": list(match.span()),
                             "source_text": match[0], "retained_text": compound,
                             "source_text_sha256": hashlib.sha256(before.encode()).hexdigest(),
                             "message": f"Check line-end hyphen: {compound}. Original spelling retained; no automatic correction."}
                candidates.append(candidate)
                block.setdefault("normalization_warnings", []).append(candidate)
        if before != after:
            block["text"] = after
            transforms.append({"block_id": block["block_id"], "before": before, "after_sha256": hashlib.sha256(after.encode()).hexdigest(), "reasons": reasons})
    # Merge only a demonstrably split word or geometry-supported continuation.
    # Keep both block identities and provenance; never fuse arbitrary paragraphs.
    nonbreak = [block for block in blocks if block["type"] != "page_break"]
    by_id = {block["block_id"]: block for block in blocks}
    physical_text = {block["block_id"]: block.get("text", "") for block in blocks}
    for left,right in zip(nonbreak, nonbreak[1:]):
        if left["type"] != "paragraph" or right["type"] != "paragraph": continue
        p,q = left["provenance"]["source_page"],right["provenance"]["source_page"]
        if q != p+1: continue
        tail = {**left, "text": physical_text[left["block_id"]]}
        a = re.search(rf"([{LETTERS}]+)-$", tail["text"])
        b = re.match(rf"([a-zа-яё][{LETTERS}]*)\b", right.get("text", ""))
        whole = a[1]+b[1] if a and b else None
        word_join = whole and words.get(whole.casefold(),0) >= 1 and not words.get((a[1]+"-"+b[1]).casefold(),0)
        if not word_join and not _continuous_prose(tail, right): continue
        owner = by_id[left["provenance"].get("text_consumed_by", left["block_id"])]
        before = owner["text"]
        continuation = right["text"]
        owner["text"] = (before[:-len(a[1])-1]+whole+continuation[len(b[1]):] if word_join else before+" "+continuation)
        owner["provenance"].setdefault("continuation_block_ids", []).append(right["block_id"])
        owner["provenance"].setdefault("source_pages", [p]).append(q)
        right["text"] = ""
        right["provenance"]["text_consumed_by"] = owner["block_id"]
        transforms.append({"block_id": owner["block_id"], "before": before, "continuation_block_id": right["block_id"], "continuation_before": continuation,
                           "after_sha256": hashlib.sha256(owner["text"].encode()).hexdigest(), "reasons": ["corpus_attested_cross_page_word" if word_join else "native_geometry_paragraph_continuation"]})
    return {"schema_version": VERSION, "policy": "reversible layout normalization after human decisions; no semantic correction", "transforms": transforms, "review_candidates": candidates}
