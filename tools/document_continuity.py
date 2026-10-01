"""Selection-aware completeness and explicit excerpt gaps, without inference."""
import json


def parse_page_spec(spec):
    if not spec:
        return None
    pages = set()
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = map(int, part.split("-", 1))
            else:
                start = end = int(part)
            if start < 1 or end < start:
                raise ValueError()
        except ValueError:
            raise ValueError(f"Некорректный номер/диапазон страниц: {part}") from None
        pages.update(range(start, end + 1))
    return pages


def selection(root, book):
    """Fail closed on missing requested pages; legacy unknown totals stay honest."""
    actual = [page.get("page") for page in book.get("pages", [])]
    if any(type(page) is not int or page < 1 for page in actual) or actual != sorted(set(actual)):
        raise ValueError("invalid/duplicate/out-of-order book page provenance")
    stored = book.get("page_selection") or {}
    expected, total = stored.get("requested_pages"), stored.get("source_total")
    authority = "book.json" if expected is not None else None
    anchor = root / "extraction/source.json"
    if book.get("extraction_policy") == "automatic-extraction-1" and anchor.is_file():
        anchor_pages = json.loads(anchor.read_text())["selected_pages"]
        if expected is not None and expected != anchor_pages:
            raise ValueError("conflicting requested page evidence")
        expected, authority = anchor_pages, "extraction/source.json"
    # Existing legacy outputs have a total in their saved run summary. This
    # is read-only recovery, not a new PDF/scan/OCR operation.
    summary = root / "run_summary.json"
    if total is None and summary.is_file():
        total = json.loads(summary.read_text()).get("pages", {}).get("source_total")
    requested = parse_page_spec(book.get("pages_filter"))
    excluded = parse_page_spec(book.get("excluded_pages")) or set()
    if expected is None and (requested is not None or type(total) is int):
        requested = requested if requested is not None else set(range(1, total+1))
        expected, authority = sorted(requested-excluded), "saved_page_selection"
    if expected is not None:
        if (not isinstance(expected, list) or not expected
                or any(type(page) is not int or page < 1 for page in expected)
                or expected != sorted(set(expected))):
            raise ValueError("invalid requested page evidence")
        if actual != expected:
            raise ValueError(f"page completeness failure: missing={sorted(set(expected)-set(actual))}, unexpected={sorted(set(actual)-set(expected))}")
        if type(total) is int and expected[-1] > total:
            raise ValueError("requested page outside source total")
    gaps = [{"after_page": a, "before_page": b, "omitted_start": a+1, "omitted_end": b-1}
            for a,b in zip(actual, actual[1:]) if b > a+1]
    return {"schema_version": "1.0", "requested_pages": expected, "source_total": total,
            "actual_pages": actual, "evidence": authority,
            "completeness": "verified" if expected is not None else "unverifiable_legacy_selection",
            "gaps": gaps}


def gap_label(gap):
    start, end = gap["omitted_start"], gap["omitted_end"]
    omitted = str(start) if start == end else f"{start}–{end}"
    return f"Excerpt break — source pages {omitted} not included. Next excerpt starts on source page {gap['before_page']}."


def selection_notice(selection):
    pages, total = selection["actual_pages"], selection["source_total"]
    if not pages or (type(total) is int and len(pages) == total):
        return None
    if total is None and not selection["gaps"]:
        return None
    ranges = []
    start = end = pages[0]
    for page in pages[1:]:
        if page == end+1:
            end = page
        else:
            ranges.append(str(start) if start == end else f"{start}–{end}")
            start = end = page
    ranges.append(str(start) if start == end else f"{start}–{end}")
    return ("Selected source pages: " + ", ".join(ranges) + (f" of {total}" if total else "")
            + ". Excerpts may begin or end within a paragraph.")
