"""Provenance-preserving, opt-in EN↔RU translation derived from BookText CDM.

This module never edits OCR, review decisions, final-book files, or the CDM.
It invokes a caller-selected local provider only when a translation command is
explicitly requested.  The resulting artifact is source anchored and can be
validated for staleness without contacting the provider again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tools import canonical_document, review_decisions


SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "booktext.translation"
CHUNK_PLAN_TYPE = "booktext.translation_chunk_plan"
CHUNK_CHECKPOINT_TYPE = "booktext.translation_chunk_checkpoint"
CHUNK_WORKSPACE_TYPE = "booktext.translation_chunk_workspace"
CHUNK_SCHEMA_VERSION = "1.0"
SUPPORTED_LANGUAGES = ("en", "ru")
TEXT_TYPES = {"heading", "paragraph", "list", "caption", "unknown"}
DEFAULT_LOGICAL_UNIT_CHUNK_SIZE = 12
# Ollama owns the shared daemon and model processes, not BookText. A request
# lease is the only safe way to bound BookText's model residency: it lets a
# sequential document reuse one loaded model, then lets Ollama unload it
# naturally. BookText must never issue a global unload or kill a process by
# name, as either action could interrupt another local client.
DEFAULT_MODEL_KEEP_ALIVE = "30s"
MAX_MODEL_KEEP_ALIVE_SECONDS = 300
# Units are deliberately limited to unambiguous compact scientific forms.  A
# following ordinary word (for example ``1/2 page``) is prose, not a unit and
# must not make an otherwise faithful translation look numerically invalid.
NUMBER_RE = re.compile(r"(?<![\w.])[+-]?(?:\d+(?:[.,]\d+)?(?:/\d+)?)(?:\s*(?:mg|g|kg|µg|mcg|ml|l|mm|cm|km|hz|khz|mhz|ghz|%|°[CF]))?", re.IGNORECASE)
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_RE = re.compile(r"[A-Za-z]")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha_file(path):
    return _sha_bytes(Path(path).read_bytes())


def _load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON artifact: {Path(path).name}") from error


def _atomic_json(path, value):
    review_decisions.atomic_write_json(Path(path), value)


def _digest(value):
    return _sha_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _translation_workspace(root):
    return Path(root).expanduser().resolve() / "translations" / "chunks"


def _plan_path(root):
    return _translation_workspace(root) / "translation_plan.json"


def _checkpoint_path(root, chunk_id):
    return _translation_workspace(root) / chunk_id / "checkpoint.json"


def _workspace_marker(root):
    return _translation_workspace(root) / ".booktext-translation-workspace.json"


def pair_name(source_language, target_language):
    if source_language not in SUPPORTED_LANGUAGES or target_language not in SUPPORTED_LANGUAGES or source_language == target_language:
        raise ValueError("translation direction must be en→ru or ru→en")
    return f"{source_language}-{target_language}"


def _validate_keep_alive(value):
    """Accept only a finite, short lease; never let callers pin a model."""
    if not isinstance(value, str):
        raise ValueError("keep_alive must be a bounded duration such as 30s")
    match = re.fullmatch(r"(\d+)(ms|s|m)", value)
    if not match:
        raise ValueError("keep_alive must be a bounded duration such as 30s")
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount / 1000 if unit == "ms" else amount * (60 if unit == "m" else 1)
    if seconds <= 0 or seconds > MAX_MODEL_KEEP_ALIVE_SECONDS:
        raise ValueError("keep_alive must be between 1ms and 300s")
    return value


def translation_path(root, source_language, target_language):
    return Path(root).expanduser().resolve() / "translations" / f"{pair_name(source_language, target_language)}.json"


def _glossary(path):
    if path is None:
        return {}, None
    path = Path(path).expanduser().resolve()
    value = _load(path)
    if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError("glossary must be a JSON object of source terms to target terms")
    return value, {"path": str(path), "sha256": sha_file(path), "entries": len(value)}


def _script_language(text):
    cyrillic, latin = len(CYRILLIC_RE.findall(text)), len(LATIN_RE.findall(text))
    if cyrillic and not latin:
        return "ru"
    if latin and not cyrillic:
        return "en"
    if cyrillic and latin:
        return "mixed"
    return "undetermined"


def _model_info(endpoint, model, timeout):
    request = urllib.request.Request(endpoint.rstrip("/") + "/api/tags", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise ValueError("local Ollama service is unavailable") from error
    choices = payload.get("models", []) if isinstance(payload, dict) else []
    found = next((item for item in choices if isinstance(item, dict) and item.get("name") == model), None)
    if not found:
        raise ValueError(f"requested local Ollama model is unavailable: {model}")
    return {"kind": "ollama", "endpoint": endpoint.rstrip("/"), "model": model,
            "model_digest": found.get("digest"), "model_size_bytes": found.get("size"), "api": "/api/chat"}


def _extract_translation(content):
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("provider did not return a translation JSON object")
        value = json.loads(match.group(0))
    translation = value.get("translation") if isinstance(value, dict) else None
    if not isinstance(translation, str) or not translation.strip():
        raise ValueError("provider response has no non-empty translation")
    return translation.strip()


def _translate_ollama(*, endpoint, model, source, source_language, target_language, before, after, glossary, timeout, keep_alive=DEFAULT_MODEL_KEEP_ALIVE, document_context=None):
    glossary_text = json.dumps(glossary, ensure_ascii=False, sort_keys=True) if glossary else "{}"
    prompt = {
        "task": "Translate only the source block. Do not correct, summarize, omit, add commentary, or translate numbers/identifiers.",
        "source_language": source_language,
        "target_language": target_language,
        "source_block": source,
        "previous_context": before,
        "next_context": after,
        "document_context": document_context or {},
        "required_glossary": glossary,
        "response": "Return exactly JSON: {\"translation\": \"...\"}",
        "safety": "Preserve all numeric tokens, fractions, units, citations and proper identifiers exactly when possible.",
    }
    # Translation needs a faithful rendered block, not a reasoning trace.
    # Qwen3 and other thinking-capable local models can otherwise spend the
    # whole bounded request window on hidden analysis before producing JSON.
    # This remains an explicit provider setting in the resulting provenance.
    payload = {"model": model, "stream": False, "format": "json", "think": False, "keep_alive": keep_alive, "options": {"temperature": 0},
               "messages": [{"role": "system", "content": "You are a faithful document translator."},
                            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]}
    request = urllib.request.Request(endpoint.rstrip("/") + "/api/chat", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise ValueError("local Ollama translation request failed") from error
    content = (((result.get("message") or {}).get("content")) if isinstance(result, dict) else None)
    if not isinstance(content, str):
        raise ValueError("local Ollama response is missing message content")
    return _extract_translation(content), {"temperature": 0, "thinking": False, "response_format": "json", "glossary_json": glossary_text,
                                           "keep_alive": keep_alive}


def _eligible(document):
    return [block for block in document["blocks"] if block.get("type") in TEXT_TYPES and isinstance(block.get("text"), str) and block["text"].strip()]


def _document_context(document, *, source_language, target_language, glossary):
    """Stable compact context that never includes neighbouring output units."""
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    return {"document_id": document.get("document_id"), "title": metadata.get("title"),
            "source_language": source_language, "target_language": target_language,
            "required_glossary": glossary,
            "output_contract": "Translate only the owned source block; supplied context is read-only and must never be emitted."}


def _unit_spec(block):
    return {"block_id": block["block_id"], "source_text_sha256": _sha_bytes(block["text"].encode("utf-8")),
            "source_page": block.get("provenance", {}).get("source_page")}


def _ensure_translation_workspace(root, *, translation_id, source_fingerprint):
    workspace = _translation_workspace(root)
    workspace.mkdir(parents=True, exist_ok=True)
    marker_path = _workspace_marker(root)
    marker = _load(marker_path)
    if marker is None:
        marker = {"schema_version": CHUNK_SCHEMA_VERSION, "artifact_type": CHUNK_WORKSPACE_TYPE,
                  "workspace_id": str(uuid.uuid4()), "translation_id": translation_id,
                  "source_fingerprint": source_fingerprint, "path": str(workspace), "created_at": _now()}
        _atomic_json(marker_path, marker)
    if marker.get("artifact_type") != CHUNK_WORKSPACE_TYPE or marker.get("path") != str(workspace):
        raise ValueError("translation checkpoint workspace ownership marker is invalid")
    return workspace


def _create_or_load_translation_plan(root, *, translation_id, source_fingerprint, units, options, chunk_size=DEFAULT_LOGICAL_UNIT_CHUNK_SIZE):
    """Own complete logical units; context units are explicitly read-only."""
    if not units or len({unit["block_id"] for unit in units}) != len(units):
        raise ValueError("translation requires ordered unique logical units")
    if not 1 <= int(chunk_size) <= 100:
        raise ValueError("translation chunk size must be between 1 and 100 logical units")
    _ensure_translation_workspace(root, translation_id=translation_id, source_fingerprint=source_fingerprint)
    path = _plan_path(root)
    existing = _load(path)
    expected = {"translation_id": translation_id, "source_fingerprint": source_fingerprint,
                "units": units, "options_fingerprint": _digest(options)}
    if existing is not None:
        if any(existing.get(key) != value for key, value in expected.items()):
            existing.update({"status": "stale", "stale_reason": "source_or_translation_options_changed", "updated_at": _now()})
            _atomic_json(path, existing)
            raise ValueError("existing translation checkpoint plan is stale; source or translation options changed")
        return existing, False
    chunks = []
    for position in range(0, len(units), int(chunk_size)):
        owned = units[position:position + int(chunk_size)]
        before, after = units[max(0, position - 1):position], units[position + int(chunk_size):position + int(chunk_size) + 1]
        chunk_id = f"chunk-{len(chunks) + 1:04d}"
        chunks.append({"chunk_id": chunk_id, "owned_unit_ids": [unit["block_id"] for unit in owned],
                       "context_before_unit_ids": [unit["block_id"] for unit in before],
                       "context_after_unit_ids": [unit["block_id"] for unit in after],
                       "owned_source_pages": sorted({unit["source_page"] for unit in owned if isinstance(unit.get("source_page"), int)}),
                       "status": "pending", "attempts": 0,
                       "artifacts": {"checkpoint": str(_checkpoint_path(root, chunk_id).relative_to(root))},
                       "validation": None, "started_at": None, "completed_at": None})
    plan = {"schema_version": CHUNK_SCHEMA_VERSION, "artifact_type": CHUNK_PLAN_TYPE, "translation_id": translation_id,
            "source_fingerprint": source_fingerprint, "units": units, "options": options,
            "options_fingerprint": _digest(options), "chunk_size": int(chunk_size), "unit_count": len(units), "status": "active",
            "chunks": chunks, "assembly": {"status": "pending"}, "created_at": _now(), "updated_at": _now()}
    _atomic_json(path, plan)
    return plan, True


def _save_plan(root, plan):
    plan["updated_at"] = _now()
    _atomic_json(_plan_path(root), plan)


def _valid_translation_checkpoint(root, chunk):
    path = Path(root).resolve() / chunk.get("artifacts", {}).get("checkpoint", "")
    if not path.is_file() or path.is_symlink():
        return None
    payload = _load(path)
    expected, entries = chunk.get("owned_unit_ids", []), payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or [entry.get("block_id") for entry in entries if isinstance(entry, dict)] != expected:
        return None
    if payload.get("artifact_type") != CHUNK_CHECKPOINT_TYPE or payload.get("chunk_id") != chunk.get("chunk_id") or payload.get("entries_sha256") != _digest(entries):
        return None
    return entries


def _commit_translation_checkpoint(root, plan, chunk, entries, *, duration_seconds):
    if [entry.get("block_id") for entry in entries] != chunk["owned_unit_ids"]:
        raise ValueError("translation checkpoint contains units outside chunk ownership")
    payload = {"schema_version": CHUNK_SCHEMA_VERSION, "artifact_type": CHUNK_CHECKPOINT_TYPE,
               "chunk_id": chunk["chunk_id"], "owned_unit_ids": chunk["owned_unit_ids"], "entries": entries,
               "entries_sha256": _digest(entries), "committed_at": _now(), "duration_seconds": duration_seconds, "lifecycle": "checkpoint"}
    _atomic_json(_checkpoint_path(root, chunk["chunk_id"]), payload)
    if _valid_translation_checkpoint(root, chunk) is None:
        raise ValueError("translation checkpoint verification failed")
    chunk.update({"status": "complete", "completed_at": _now(), "duration_seconds": duration_seconds,
                  "validation": {"status": "PASS", "entries_sha256": payload["entries_sha256"]}})
    _save_plan(root, plan)


def _compact_translation_checkpoints(root, plan):
    workspace = _translation_workspace(root).resolve(); marker = _load(_workspace_marker(root))
    if not marker or marker.get("path") != str(workspace) or plan.get("assembly", {}).get("status") != "complete":
        return {"cleanup_allowed": False, "files_deleted": 0, "bytes_deleted": 0}
    deleted = files = 0
    for chunk in plan.get("chunks", []):
        target = Path(root).resolve() / chunk.get("artifacts", {}).get("checkpoint", "")
        if target.is_relative_to(workspace) and target.is_file() and not target.is_symlink():
            deleted += target.stat().st_size; files += 1; target.unlink(); chunk.setdefault("artifacts", {})["checkpoint_compacted"] = True
    plan["compaction"] = {"completed_at": _now(), "files_deleted": files, "bytes_deleted": deleted,
                          "retained_categories": ["canonical", "evidence", "compact_checkpoint_metadata"]}
    _save_plan(root, plan)
    return {"cleanup_allowed": True, "files_deleted": files, "bytes_deleted": deleted}


def _numbers(text):
    return NUMBER_RE.findall(text)


def quality_signals(source, translated, *, source_language, target_language, kind="paragraph"):
    """Review evidence only; neither fixes text nor certifies model semantics."""
    signals = []
    if _numbers(source) != _numbers(translated): signals.append("numeric_tokens_changed")
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё]+", translated)
    if any(re.search(r"[A-Za-z]", token) and re.search(r"[А-Яа-яЁё]", token) for token in tokens):
        signals.append("mixed_scripts_inside_word")
    if kind == "paragraph" and len(tokens) >= 12:
        target_pattern = r"[А-Яа-яЁё]" if target_language == "ru" else r"[A-Za-z]"
        target_count = sum(bool(re.search(target_pattern, token)) for token in tokens)
        if target_count / len(tokens) < .2: signals.append("possible_untranslated_prose")
    if re.search(r"[A-Za-zА-Яа-яЁё]-\s*$", source): signals.append("source_boundary_hyphen_requires_review")
    return signals


def _entry(block, *, source_language, target_language, text, before, after, glossary, provider, timeout, keep_alive, document_context=None):
    detected = _script_language(text)
    base = {"block_id": block["block_id"], "block_type": block["type"], "source_page": block.get("provenance", {}).get("source_page"),
            "source_text": text, "source_text_sha256": _sha_bytes(text.encode("utf-8")), "detected_language": detected,
            "context": {"previous_block_id": before[0] if before else None, "next_block_id": after[0] if after else None}}
    # A block already wholly in the target language needs no model call. Mixed
    # blocks are sent with an explicit source language and remain auditable.
    if detected == target_language:
        return {**base, "status": "skipped_already_target_language", "translation": None, "validation": {"numeric_tokens": "not_applicable"}}
    translated, settings = _translate_ollama(endpoint=provider["endpoint"], model=provider["model"], source=text,
        source_language=source_language, target_language=target_language, before=before[1] if before else "", after=after[1] if after else "", glossary=glossary, timeout=timeout, keep_alive=keep_alive, document_context=document_context)
    original_numbers, translated_numbers = _numbers(text), _numbers(translated)
    status = "machine_translated" if original_numbers == translated_numbers else "needs_numeric_review"
    return {**base, "status": status, "translation": translated, "translation_sha256": _sha_bytes(translated.encode("utf-8")),
            "validation": {"numeric_tokens": "PASS" if status == "machine_translated" else "FAIL", "source": original_numbers, "translation": translated_numbers,
                           "quality_signals": quality_signals(text, translated, source_language=source_language, target_language=target_language, kind=block["type"])},
            "provider_settings": settings}


def _render(entries, *, bilingual=False):
    lines = []
    for item in entries:
        source, translated, kind = item["source_text"], item.get("translation"), item["block_type"]
        if kind == "heading":
            lines.extend(["", "## " + (translated or source)])
            if bilingual and translated: lines.append("\n> " + source)
        elif kind == "list":
            lines.extend(["", "- " + (translated or source)])
            if bilingual and translated: lines.append("  - " + source)
        else:
            lines.extend(["", translated or source])
            if bilingual and translated: lines.extend(["", "> " + source.replace("\n", "\n> ")])
    return "\n".join(lines).lstrip() + "\n"


def _validate_manifest(manifest, document, *, cdm_sha=None):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("artifact_type") != ARTIFACT_TYPE:
        raise ValueError("invalid translation schema")
    source = manifest.get("source") or {}
    # The file fingerprint anchors this derivative to the exact CDM artifact
    # used for translation, including any future schema-preserving change.
    stale = cdm_sha is not None and source.get("cdm_sha256") != cdm_sha
    expected = {block["block_id"]: block for block in _eligible(document)}
    observed = manifest.get("blocks")
    if not isinstance(observed, list) or len(observed) != len(expected) or [item.get("block_id") for item in observed if isinstance(item, dict)] != [block["block_id"] for block in _eligible(document)]:
        raise ValueError("translation does not cover the current eligible CDM blocks")
    invalid = []
    quality = []
    for item in observed:
        block = expected[item["block_id"]]
        if item.get("source_text_sha256") != _sha_bytes(block["text"].encode("utf-8")):
            stale = True
        if item.get("status") in {"machine_translated", "needs_numeric_review"} and not isinstance(item.get("translation"), str):
            invalid.append(item["block_id"])
        if isinstance(item.get("translation"), str):
            direction = manifest.get("direction") or {}
            signals = quality_signals(block["text"], item["translation"], source_language=direction.get("source_language", "en"),
                                      target_language=direction.get("target_language", "ru"), kind=block["type"])
            if signals: quality.append({"block_id": block["block_id"], "source_page": block.get("provenance", {}).get("source_page"), "signals": signals})
    return {"valid": not invalid, "stale": stale, "invalid_block_ids": invalid,
            "machine_translated": sum(item.get("status") == "machine_translated" for item in observed),
            "needs_numeric_review": sum(item.get("status") == "needs_numeric_review" for item in observed),
            "skipped": sum(str(item.get("status", "")).startswith("skipped_") for item in observed),
            "quality_policy": "signals are human-review evidence, not automatic corrections", "quality_review": quality, "quality_review_count": len(quality)}


def _reset_translation_workspace(root):
    """Discard only a verified BookText-owned translation checkpoint workspace."""
    workspace = _translation_workspace(root).resolve()
    marker = _load(_workspace_marker(root))
    if workspace.exists():
        if not marker or marker.get("artifact_type") != CHUNK_WORKSPACE_TYPE or marker.get("path") != str(workspace):
            raise ValueError("refusing to reset unauthenticated translation checkpoint workspace")
        shutil.rmtree(workspace)


def translate_document(output_dir, *, source_language, target_language, provider, model, glossary_path=None, endpoint="http://127.0.0.1:11434", timeout=120, keep_alive=DEFAULT_MODEL_KEEP_ALIVE, chunk_size=DEFAULT_LOGICAL_UNIT_CHUNK_SIZE, restart=False, progress=None):
    root = Path(output_dir).expanduser().resolve()
    pair = pair_name(source_language, target_language)
    if provider != "ollama":
        raise ValueError("only explicit local Ollama translation is currently supported")
    document = _load(root / "book_document.json")
    decisions = _load(root / "review_decisions.json", {})
    canonical_document.validate_document(document, root=root, decisions=decisions)
    glossary, glossary_provenance = _glossary(glossary_path)
    cdm_sha = sha_file(root / "book_document.json")
    keep_alive = _validate_keep_alive(keep_alive)
    destination = translation_path(root, source_language, target_language)
    existing = _load(destination)
    if not restart and existing and (existing.get("source") or {}).get("cdm_sha256") == cdm_sha and (existing.get("provider") or {}).get("model") == model and existing.get("glossary") == glossary_provenance:
        validation = _validate_manifest(existing, document, cdm_sha=cdm_sha)
        if validation["valid"] and not validation["stale"]:
            return {"path": str(destination), "reused": True, "validation": validation}
    if existing:
        history = destination.parent / "history" / f"{pair}-{(existing.get('source') or {}).get('cdm_sha256', 'unknown')[:12]}.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        if not history.exists(): _atomic_json(history, existing)
    # A changed/stale derived manifest is archived above. Its checkpoint plan
    # belongs to the old source fingerprint and cannot be resumed safely.
    if restart or existing:
        _reset_translation_workspace(root)
    metadata = {**_model_info(endpoint, model, timeout), "resource_lifecycle": {
        "keep_alive": keep_alive,
        "policy": "BookText requests a bounded Ollama model lease; it never stops the shared daemon or unloads a model by process name.",
    }}
    blocks = _eligible(document)
    translation_id = "translation-" + _sha_bytes((document.get("document_id", "") + pair + cdm_sha).encode("utf-8"))[:20]
    options = {"direction": {"source_language": source_language, "target_language": target_language}, "provider": "ollama", "model": model,
               "model_digest": metadata.get("model_digest"), "glossary": glossary_provenance, "keep_alive": keep_alive}
    units = [_unit_spec(block) for block in blocks]
    source_fingerprint = _digest({"cdm_sha256": cdm_sha, "units": units, "options": options})
    plan, plan_created = _create_or_load_translation_plan(root, translation_id=translation_id, source_fingerprint=source_fingerprint,
        units=units, options=options, chunk_size=chunk_size)
    if progress:
        progress("chunk_plan_created" if plan_created else "job_resumed", {"chunks": len(plan["chunks"]), "logical_units": len(units), "translation_id": translation_id})
    by_id = {block["block_id"]: block for block in blocks}
    index_by_id = {block["block_id"]: index for index, block in enumerate(blocks)}
    context = _document_context(document, source_language=source_language, target_language=target_language, glossary=glossary)
    resumed_chunks = []
    for chunk in plan["chunks"]:
        checkpoint = _valid_translation_checkpoint(root, chunk)
        if chunk.get("status") == "complete" and checkpoint is not None:
            resumed_chunks.append(chunk["chunk_id"])
            if progress: progress("chunk_progress", {"chunk_id": chunk["chunk_id"], "status": "resumed", "current": len(resumed_chunks), "total": len(plan["chunks"]), "logical_units": len(chunk["owned_unit_ids"])})
            continue
        chunk.update({"status": "running", "attempts": int(chunk.get("attempts", 0)) + 1, "started_at": _now()}); _save_plan(root, plan)
        if progress: progress("chunk_started", {"chunk_id": chunk["chunk_id"], "current": plan["chunks"].index(chunk) + 1, "total": len(plan["chunks"]), "logical_units": len(chunk["owned_unit_ids"]), "owned_unit_ids": chunk["owned_unit_ids"]})
        started = time.monotonic(); entries = []
        try:
            for block_id in chunk["owned_unit_ids"]:
                block = by_id[block_id]; index = index_by_id[block_id]
                before = (blocks[index - 1]["block_id"], blocks[index - 1]["text"][-700:]) if index else None
                after = (blocks[index + 1]["block_id"], blocks[index + 1]["text"][:700]) if index + 1 < len(blocks) else None
                entries.append(_entry(block, source_language=source_language, target_language=target_language, text=block["text"], before=before, after=after,
                    glossary=glossary, provider=metadata, timeout=timeout, keep_alive=keep_alive, document_context=context))
            _commit_translation_checkpoint(root, plan, chunk, entries, duration_seconds=round(time.monotonic() - started, 3))
            if progress: progress("chunk_completed", {"chunk_id": chunk["chunk_id"], "current": plan["chunks"].index(chunk) + 1, "total": len(plan["chunks"]), "logical_units": len(entries), "checkpoint": chunk["artifacts"]["checkpoint"]})
        except BaseException as error:
            chunk.update({"status": "failed", "last_error": str(error), "failed_at": _now()}); _save_plan(root, plan)
            if progress: progress("chunk_failed", {"chunk_id": chunk["chunk_id"], "current": plan["chunks"].index(chunk) + 1, "total": len(plan["chunks"]), "error": str(error)})
            raise
    if progress: progress("assembly_started", {"chunks": len(plan["chunks"]), "logical_units": len(units)})
    assembled = []
    for chunk in plan["chunks"]:
        checkpoint = _valid_translation_checkpoint(root, chunk)
        if checkpoint is None:
            raise ValueError("translation assembly is missing a validated chunk checkpoint")
        assembled.extend(checkpoint)
    expected_ids = [block["block_id"] for block in blocks]
    if [entry.get("block_id") for entry in assembled] != expected_ids:
        raise ValueError("translation assembly failed logical-unit coverage or ordering validation")
    entries = assembled
    destination.parent.mkdir(parents=True, exist_ok=True)
    target_markdown = destination.parent / f"book-{pair}.md"
    bilingual_markdown = destination.parent / f"book-{pair}-bilingual.md"
    target_markdown.write_text(_render(entries), encoding="utf-8")
    bilingual_markdown.write_text(_render(entries, bilingual=True), encoding="utf-8")
    manifest = {"schema_version": SCHEMA_VERSION, "artifact_type": ARTIFACT_TYPE, "translation_id": translation_id,
        "created_at": _now(), "direction": {"source_language": source_language, "target_language": target_language},
        "source": {"document_id": document.get("document_id"), "cdm_path": "book_document.json", "cdm_sha256": cdm_sha, "raw_ocr_mutated": False, "decision_provenance": document.get("review", {})},
        "provider": metadata, "glossary": glossary_provenance, "blocks": entries,
        "execution": {"logical_unit_ownership": True, "document_context": context, "checkpoint_plan": str(_plan_path(root).relative_to(root)),
                      "source_fingerprint": source_fingerprint, "chunk_count": len(plan["chunks"]), "resumed_chunks": resumed_chunks,
                      "assembly": {"coverage": "PASS", "ordering": "PASS", "duplicates": "PASS", "context_only_output": "PASS"}},
        "outputs": {"translation_markdown": str(target_markdown.relative_to(root)), "bilingual_markdown": str(bilingual_markdown.relative_to(root))},
        "policy": "Derived translation only. Raw OCR, review decisions, final-book artifacts, and CDM remain unchanged. Machine translation is not human-reviewed automatically."}
    manifest["validation"] = _validate_manifest(manifest, document, cdm_sha=cdm_sha)
    _atomic_json(destination, manifest)
    plan.update({"status": "complete", "assembly": {"status": "complete", "validation": manifest["execution"]["assembly"], "completed_at": _now()}}); _save_plan(root, plan)
    cleanup = _compact_translation_checkpoints(root, plan)
    if progress:
        progress("assembly_completed", {"chunks": len(plan["chunks"]), "logical_units": len(entries), "validation": manifest["execution"]["assembly"]})
        progress("cleanup_completed", cleanup)
    return {"path": str(destination), "reused": False, "validation": manifest["validation"], "outputs": manifest["outputs"],
            "execution": {"plan_created": plan_created, "chunks": len(plan["chunks"]), "resumed_chunks": resumed_chunks, "cleanup": cleanup}}


def validate_translation(output_dir, *, source_language, target_language):
    root = Path(output_dir).expanduser().resolve()
    manifest = _load(translation_path(root, source_language, target_language))
    if manifest is None:
        raise ValueError("translation artifact does not exist")
    document = _load(root / "book_document.json")
    result = _validate_manifest(manifest, document, cdm_sha=sha_file(root / "book_document.json"))
    return {"path": str(translation_path(root, source_language, target_language)), **result}
