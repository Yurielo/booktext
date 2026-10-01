"""Reversible publication whitespace projection, not OCR/semantic correction."""
import hashlib
import re
from collections import Counter

VERSION = "1.0"
LETTERS = r"A-Za-zА-Яа-яЁё"


def vocabulary(texts):
    return Counter(word.casefold() for text in texts for word in re.findall(rf"[{LETTERS}]+(?:-[{LETTERS}]+)*", text))


def normalize(text, *, kind, trusted_lines=False, words=None):
    words = words or {}
    if kind not in {"paragraph", "heading", "caption"}:
        return text, []
    lines = text.splitlines()
    # Lists, columns, verse and numeric rows are not prose wrapping evidence.
    protected = any(re.match(r"\s*(?:[-•*]|\d+[.)])\s", line) or "\t" in line or re.search(r"\S  {2,}\S", line) for line in lines)
    prose = trusted_lines or (len(lines) > 1 and sum(len(line) >= 40 for line in lines[:-1]) >= len(lines[:-1])*.8)
    if protected or not prose:
        return text, []
    value = text
    signals = []
    def join(match):
        left, right = match.group(1), match.group(2)
        whole, compound = left+right, left+"-"+right
        if words.get(whole.casefold(), 0) >= 2 and not words.get(compound.casefold(), 0):
            signals.append("corpus_attested_line_hyphen_join")
            return whole
        signals.append("preserved_ambiguous_hyphen")
        return compound
    value = re.sub(rf"([{LETTERS}]+)-\n\s*([a-zа-яё][{LETTERS}]*)", join, value)
    value = re.sub(r"\u00ad\n\s*", "", value)
    value = re.sub(r"(?<!\n)\n(?!\n)", " ", value)
    # Letter/comma/letter only. Numeric commas, fractions and identifiers remain.
    value = re.sub(rf"(?<=[{LETTERS}]),(?=[{LETTERS}])", ", ", value)
    if value != text:
        signals.append("prose_layout_whitespace")
    return value, sorted(set(signals))


def project(blocks, source_texts):
    words = vocabulary(source_texts)
    transforms = []
    for block in blocks:
        before = block.get("text", "")
        after, reasons = normalize(before, kind=block["type"], trusted_lines=block.get("provenance", {}).get("layout_label") == "Text", words=words)
        if before != after:
            block["text"] = after
            transforms.append({"block_id": block["block_id"], "before": before, "after_sha256": hashlib.sha256(after.encode()).hexdigest(), "reasons": reasons})
    # Merge only a demonstrably split word across consecutive source pages.
    # Keep both block identities and provenance; never fuse arbitrary paragraphs.
    nonbreak = [block for block in blocks if block["type"] != "page_break"]
    for left,right in zip(nonbreak, nonbreak[1:]):
        if left["type"] != "paragraph" or right["type"] != "paragraph": continue
        p,q = left["provenance"]["source_page"],right["provenance"]["source_page"]
        if q != p+1: continue
        a = re.search(rf"([{LETTERS}]+)-$", left.get("text", ""))
        b = re.match(rf"([a-zа-яё][{LETTERS}]*)\b", right.get("text", ""))
        if not a or not b: continue
        whole = a[1]+b[1]
        if words.get(whole.casefold(),0) < 2 or words.get((a[1]+"-"+b[1]).casefold(),0): continue
        before = left["text"]
        continuation = right["text"]
        left["text"] = before[:-len(a[1])-1]+whole+continuation[len(b[1]):]
        left["provenance"]["continuation_block_ids"] = [right["block_id"]]
        left["provenance"]["source_pages"] = [p,q]
        right["text"] = ""
        right["provenance"]["text_consumed_by"] = left["block_id"]
        transforms.append({"block_id": left["block_id"], "before": before, "continuation_block_id": right["block_id"], "continuation_before": continuation,
                           "after_sha256": hashlib.sha256(left["text"].encode()).hexdigest(), "reasons": ["corpus_attested_cross_page_word"]})
    return {"schema_version": VERSION, "policy": "reversible layout normalization after human decisions; no semantic correction", "transforms": transforms}
