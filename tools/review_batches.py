"""Explicit, all-or-nothing batch confirmation over item-level decisions."""

import hashlib
import json
from pathlib import Path

from tools import review_decisions


ALLOWED = {"keep_surya", "accept_vision", "ignore"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def _load(priority_path, book_path, decisions_path):
    priority = review_decisions.load_json(priority_path); book = review_decisions.load_json(book_path)
    items = review_decisions._priority_index(priority); pages = review_decisions._page_index(book)
    document = review_decisions.load_json(decisions_path) if Path(decisions_path).exists() else review_decisions.empty_decisions(priority_path, book_path)
    return priority, book, items, pages, document


def preview(cluster, items, action, excluded_ids=()):
    if not cluster.get("batch_eligible"): raise ValueError("cluster is not batch eligible")
    if action not in cluster.get("allowed_batch_actions", []): raise ValueError("action is not allowed for this cluster")
    excluded = set(excluded_ids)
    allowed_members = cluster["eligible_member_ids"]
    if not excluded.issubset(set(allowed_members)):
        raise ValueError("only eligible cluster members may be excluded")
    selected = [item_id for item_id in allowed_members if item_id not in excluded]
    if len(selected) < 2: raise ValueError("at least two members must remain for a batch")
    if any(item_id not in items for item_id in selected): raise ValueError("cluster references unknown item")
    expected_pair = tuple(cluster.get("equivalence_signature") or ())
    current_pairs = {(str(items[item_id].get("surya") or "").strip(), str(items[item_id].get("vision") or "").strip()) for item_id in selected}
    if len(expected_pair) != 2 or current_pairs != {expected_pair}:
        raise ValueError("cluster evidence is stale or no longer exactly equivalent")
    batch_id = "batch-" + _digest({"cluster": cluster["cluster_id"], "action": action, "members": selected})
    return {"batch_id": batch_id, "cluster_id": cluster["cluster_id"], "action": action, "member_ids": selected, "excluded_member_ids": sorted(excluded), "requires_confirmation": True}


def _candidate(item, page, action, batch):
    decision = {
        "decision_id": _digest({"item_id": item["item_id"], "action": action, "batch_id": batch["batch_id"]}),
        "item_id": item["item_id"], "page": item["page"], "action": action,
        "source_anchor": {"priority_item_sha256": review_decisions.item_fingerprint(item), "page_text_sha256": review_decisions.page_fingerprint(page)},
        "batch_provenance": {"batch_id": batch["batch_id"], "cluster_id": batch["cluster_id"], "action": action, "eligibility_version": "1.0"},
    }
    return decision


def apply_batch(*, cluster, priority_path, book_path, decisions_path, action, excluded_ids=(), confirmed=False):
    """Validate every item before atomically writing any ordinary decisions."""
    if confirmed is not True: raise ValueError("explicit batch confirmation is required")
    _, _, items, pages, document = _load(priority_path, book_path, decisions_path)
    plan = preview(cluster, items, action, excluded_ids)
    existing = document.get("decisions", [])
    existing_ids = {entry.get("item_id") for entry in existing if isinstance(entry, dict)}
    candidates, failures = [], []
    for item_id in plan["member_ids"]:
        if item_id in existing_ids:
            failures.append({"item_id": item_id, "reason": "existing_item_decision"}); continue
        candidate = _candidate(items[item_id], pages[items[item_id]["page"]], action, plan)
        status, reason, _ = review_decisions.validate_decision(candidate, items, pages)
        if status in {"invalid", "stale", "conflict"}: failures.append({"item_id": item_id, "reason": reason})
        else: candidates.append(candidate)
    if failures or len(candidates) != len(plan["member_ids"]):
        return {"applied": False, "batch_id": plan["batch_id"], "failures": failures, "member_ids": plan["member_ids"]}
    updated = dict(document); updated["decisions"] = sorted([*existing, *candidates], key=lambda entry: entry["item_id"])
    review_decisions.atomic_write_json(decisions_path, updated)
    return {"applied": True, "batch_id": plan["batch_id"], "member_ids": plan["member_ids"], "decision_count": len(candidates)}


def revert_batch(*, decisions_path, batch_id):
    document = review_decisions.load_json(decisions_path)
    original = document.get("decisions", [])
    retained = [entry for entry in original if (entry.get("batch_provenance") or {}).get("batch_id") != batch_id]
    removed = len(original) - len(retained)
    if not removed: raise ValueError("batch provenance was not found")
    document["decisions"] = retained
    review_decisions.atomic_write_json(decisions_path, document)
    return {"batch_id": batch_id, "reverted_item_count": removed}
