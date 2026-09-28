#!/usr/bin/env python3
"""Conservative, deterministic organizational clusters for priority evidence."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "booktext.review_clusters"
PRIORITIES = ("HIGH", "MEDIUM", "LOW", "INFO")
_PUNCT = re.compile(r"[^\w\s\-‐‑‒–—]", re.UNICODE)
_LATIN = re.compile(r"[A-Za-z]")
_UNSAFE = re.compile(r"\d|[+−±=/\\%‰°]|\b(?:mg|ml|kg|km|cm|mm|hz|khz|mhz|ghz|mv|kv|ma|ua|db)\b", re.I)


def _compact(value): return re.sub(r"\s+", " ", str(value or "")).strip()
def _case_punct(value): return _PUNCT.sub("", _compact(value)).casefold()
def _segmented(value): return re.sub(r"[\s\-‐‑‒–—]+", "", _case_punct(value))
def _digest(value): return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def classify(item):
    """Return a safe cluster category/signature or a deliberately singleton one."""
    item_type = item.get("item_type")
    signals = item.get("signals") or {}
    kind = str(signals.get("qa_kind", "")).lower()
    category = str(signals.get("qa_category", "")).lower()
    surya, vision = _compact(item.get("surya")), _compact(item.get("vision"))
    protected = item_type == "table_fallback_review" or kind in {"insert", "delete"} or category == "completeness"
    if protected: return "structural_fallback" if item_type == "table_fallback_review" else "completeness", None
    if _UNSAFE.search(surya) or _UNSAFE.search(vision): return "numeric_fraction_unit", None
    if _LATIN.search(surya) or _LATIN.search(vision): return "scientific_or_latin", None
    if not surya or not vision: return "unique_other", None
    if _case_punct(surya) == _case_punct(vision) and _case_punct(surya):
        if _compact(surya).casefold() == _compact(vision).casefold():
            return "punctuation_case", ("punctuation_case", _case_punct(surya))
    if _segmented(surya) == _segmented(vision) and _segmented(surya):
        return "segmentation_whitespace", ("segmentation", _segmented(surya))
    return "repeated_lexical_pair", ("lexical_pair", surya, vision)


def build_clusters(priority):
    if priority.get("artifact_type") != "booktext.review_priority" or priority.get("schema_version") != "1.0":
        raise ValueError("unsupported review priority artifact")
    items = priority.get("items")
    if not isinstance(items, list): raise ValueError("priority items must be a list")
    groups = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("item_id"): raise ValueError("invalid priority item")
        category, signature = classify(item)
        key = (category, signature) if signature else (category, item["item_id"])
        groups.setdefault(key, []).append(item)
    clusters = []
    for (category, signature), members in groups.items():
        members.sort(key=lambda value: value["item_id"])
        representative = members[0]
        cluster_id = f"cluster-{_digest({'category': category, 'signature': signature, 'members': [x['item_id'] for x in members]})}"
        distribution = {priority_name: sum(x.get("priority") == priority_name for x in members) for priority_name in PRIORITIES}
        clusters.append({
            "cluster_id": cluster_id, "category": category, "member_item_ids": [x["item_id"] for x in members],
            "member_count": len(members), "representative": {key: representative.get(key) for key in ("item_id", "page", "priority", "surya", "vision", "reasons")},
            "signature": list(signature) if signature else None, "safety_flags": ["organizational_only", "no_bulk_decision"] + (["intentionally_singleton"] if len(members) == 1 else []),
            "priority_distribution": distribution, "pages": sorted({x["page"] for x in members}), "grouping_eligible": signature is not None,
        })
    clusters.sort(key=lambda value: (min(PRIORITIES.index(p) for p, n in value["priority_distribution"].items() if n), -value["member_count"], value["cluster_id"]))
    multi = [cluster for cluster in clusters if cluster["member_count"] > 1]
    return {
        "schema_version": SCHEMA_VERSION, "artifact_type": ARTIFACT_TYPE,
        "policy": "Clusters are organizational evidence only. Item IDs and item decisions remain source of truth; no decision propagates.",
        "source_item_count": len(items), "cluster_count": len(clusters),
        "metrics": {"singleton_clusters": len(clusters) - len(multi), "multi_item_clusters": len(multi), "items_in_multi_item_clusters": sum(x["member_count"] for x in multi), "estimated_reviewer_units": len(clusters)},
        "clusters": clusters,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--priority", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); artifact = build_clusters(json.loads(args.priority.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__": main()
