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


SCHEMA_VERSION = "1.1"
ARTIFACT_TYPE = "booktext.translation"
CHUNK_PLAN_TYPE = "booktext.translation_chunk_plan"
CHUNK_CHECKPOINT_TYPE = "booktext.translation_chunk_checkpoint"
PARTIAL_CHECKPOINT_TYPE = "booktext.translation_partial_chunk_checkpoint"
CHUNK_WORKSPACE_TYPE = "booktext.translation_chunk_workspace"
CHUNK_SCHEMA_VERSION = "1.1"
SUPPORTED_LANGUAGES = ("en", "ru")
TEXT_TYPES = {"heading", "paragraph", "list", "caption", "unknown"}
DEFAULT_LOGICAL_UNIT_CHUNK_SIZE = 12
PROMPT_VERSION = "qwen-document-translation-v2"
RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {"translation": {"type": "string"}},
    "required": ["translation"],
    "additionalProperties": False,
}
QUALITY_POLICY_VERSION = "translation-quality-v2"
MAX_UNIT_ATTEMPTS = 3
MAX_PROVIDER_REQUEST_ATTEMPTS = 3
MAX_PROVIDER_REQUEST_TIMEOUT_SECONDS = 300
CONTEXT_CHAR_BUDGET = 900
MAX_RETRY_SOURCE_CHARS = 1000
MAX_OUTPUT_TOKENS = 2048
SUPPORTED_SCHEMA_VERSIONS = {"1.0", SCHEMA_VERSION}
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


def _partial_checkpoint_path(root, chunk_id):
    return _translation_workspace(root) / chunk_id / "partial-checkpoint.json"


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


def _effective_request_keep_alive(requested, timeout):
    """Keep the bounded Ollama lease alive for the longest bounded retry.

    Ollama begins its keep-alive countdown while a request is in flight on
    some local builds.  A 30 second lease can therefore evict a model during
    a legitimate 120 second translation request.  This remains a finite
    lease, never an unload/kill operation, and is recorded separately from
    the user's requested idle lease.
    """
    match = re.fullmatch(r"(\d+)(ms|s|m)", requested)
    amount, unit = int(match.group(1)), match.group(2)
    requested_seconds = amount / 1000 if unit == "ms" else amount * (60 if unit == "m" else 1)
    required_seconds = min(MAX_MODEL_KEEP_ALIVE_SECONDS, int(timeout) * MAX_PROVIDER_REQUEST_ATTEMPTS)
    return f"{int(max(requested_seconds, required_seconds))}s"


def translation_path(root, source_language, target_language):
    return Path(root).expanduser().resolve() / "translations" / f"{pair_name(source_language, target_language)}.json"


def _glossary(path):
    if path is None:
        return {}, None
    path = Path(path).expanduser().resolve()
    value = _load(path)
    if not isinstance(value, dict):
        raise ValueError("glossary must be a JSON object of source terms to target terms")
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("glossary source terms must be non-empty strings")
        if isinstance(item, str) and item.strip():
            continue
        if isinstance(item, dict) and isinstance(item.get("target"), str) and item["target"].strip() and all(isinstance(v, str) and v.strip() for v in item.get("variants", [])):
            continue
        raise ValueError("glossary values must be target strings or {target, variants} objects")
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


def _strip_model_thinking(content):
    """Remove only an explicit Qwen-style hidden-thinking envelope."""
    return re.sub(r"^\s*<think>.*?</think>\s*", "", content or "", flags=re.DOTALL)


def _max_output_tokens_for_source(source):
    """A logical-unit-sized completion cap prevents headings using prose budget."""
    return min(MAX_OUTPUT_TOKENS, max(128, len(source) * 2))


def _is_short_heading_request(source, document_context):
    return isinstance(document_context, dict) and document_context.get("block_type") == "heading" and len(source.strip()) <= 120


def _safe_plain_provider_translation(content, *, source, target_language):
    """Accept a narrowly safe non-JSON answer only as review evidence."""
    candidate = (content or "").strip()
    if not candidate or "\n" in candidate or len(candidate) > max(120, len(source) * 4):
        return None
    if re.search(r"\b(i cannot|sorry|translation|here is|ответ|перевод)\b", candidate, re.IGNORECASE):
        return None
    expected = CYRILLIC_RE if target_language == "ru" else re.compile(r"[A-Za-z]")
    return candidate if expected.search(candidate) else None


def _sentences(text, *, tail=False, limit=CONTEXT_CHAR_BUDGET):
    """Return compact whole-sentence context; never silently split a sentence."""
    text = (text or "").strip()
    if not text:
        return ""
    parts = re.findall(r"[^.!?\n]+[.!?]+(?:\s+|$)|[^.!?\n]+$", text, flags=re.DOTALL)
    if not parts:
        return ""
    selected, used = [], 0
    iterable = reversed(parts) if tail else iter(parts)
    for part in iterable:
        part = part.strip()
        if not part:
            continue
        # A context window must be a whole sentence *and* bounded.  If the
        # nearest sentence is itself too large, omit it explicitly instead of
        # cutting it mid-sentence or silently exceeding the prompt budget.
        if used + len(part) + 1 > limit:
            if selected:
                break
            continue
        selected.append(part); used += len(part) + 1
    if tail: selected.reverse()
    return " ".join(selected)


def _heading_lineage(blocks, index):
    """Use only actual preceding heading text; never infer an unknown hierarchy."""
    lineage = []
    for candidate in reversed(blocks[:index]):
        if candidate.get("type") == "heading" and candidate.get("text"):
            lineage.append({"block_id": candidate["block_id"], "text": candidate["text"], "level": candidate.get("level")})
            if len(lineage) == 3: break
    return list(reversed(lineage))


def _glossary_rules(glossary, source):
    """Return only applicable glossary rules, preserving legacy string values."""
    result = []
    for term, value in glossary.items():
        if term.casefold() not in source.casefold():
            continue
        if isinstance(value, str):
            target, variants = value, []
        else:
            target, variants = value["target"], value.get("variants", [])
        result.append({"source": term, "target": target, "variants": variants})
    return result


def _target_term_present(text, term):
    """Allow Russian inflection through word stems; do not rewrite output."""
    words = re.findall(r"[А-Яа-яЁёA-Za-z]+", term.casefold())
    actual = text.casefold()
    if not words:
        return True
    for word in words:
        stem = word[:-2] if len(word) >= 6 and CYRILLIC_RE.search(word) else word
        if stem not in actual:
            return False
    return True


def _response_quality(source, translated, *, source_language, target_language, kind, glossary, before, after, response):
    signals = quality_signals(source, translated, source_language=source_language, target_language=target_language, kind=kind)
    if isinstance(response, dict) and response.get("response_format_fallback"):
        signals.append("provider_response_format_requires_review")
    completion = response.get("done_reason") if isinstance(response, dict) else None
    if completion and completion not in {"stop"}:
        signals.append("provider_completion_requires_review")
    if kind == "heading" and _script_language(source) == source_language and _script_language(translated) == source_language:
        signals.append("untranslated_heading")
    for rule in _glossary_rules(glossary, source):
        permitted = [rule["target"], *rule["variants"]]
        if not any(_target_term_present(translated, candidate) for candidate in permitted):
            signals.append("required_glossary_term_missing:" + rule["source"])
    compact = re.sub(r"\s+", " ", translated.casefold()).strip()
    for context in (before, after):
        sample = _sentences(context, tail=bool(before), limit=280)
        normalized = re.sub(r"\s+", " ", sample.casefold()).strip()
        if len(normalized) >= 80 and normalized in compact:
            signals.append("possible_context_leakage")
    sentences = re.findall(r"[^.!?]+[.!?]", translated)
    if len(sentences) > 1 and len({re.sub(r"\s+", " ", sentence).strip().casefold() for sentence in sentences}) < len(sentences):
        signals.append("repeated_output_sentence")
    return sorted(set(signals))


def _retryable_quality_signals(signals):
    """Only ask the model again for defects a fresh rendering can improve.

    Source-side evidence (for example a line-end hyphen inherited from the
    CDM) remains visible for review but must not waste two extra model calls.
    """
    retryable = {
        "numeric_tokens_changed",
        "mixed_scripts_inside_word",
        "possible_untranslated_prose",
        "provider_completion_requires_review",
        "untranslated_heading",
        "possible_context_leakage",
        "repeated_output_sentence",
    }
    return [signal for signal in signals if signal in retryable or signal.startswith("required_glossary_term_missing:")]


class TranslationProviderRequestError(ValueError):
    """A transient local-provider request failure safe to retry for one unit.

    This deliberately represents a request, not a process.  BookText never
    reaches into Ollama's shared daemon or models: a caller can retry the same
    source-anchored unit, while checkpoints keep every completed unit intact.
    """


def _translate_ollama(*, endpoint, model, source, source_language, target_language, before, after, glossary, timeout, keep_alive=DEFAULT_MODEL_KEEP_ALIVE, document_context=None):
    glossary_text = json.dumps(glossary, ensure_ascii=False, sort_keys=True) if glossary else "{}"
    prompt = {
        "prompt_version": PROMPT_VERSION,
        "task": "Translate only owned_source. Do not correct, summarize, omit, add commentary, or translate numbers/identifiers.",
        "source_language": source_language,
        "target_language": target_language,
        "owned_source": source,
        "context_before_read_only": before,
        "context_after_read_only": after,
        "document_context": document_context or {},
        "applicable_glossary": _glossary_rules(glossary, source),
        "response": "Return exactly JSON: {\"translation\": \"...\"}",
        "safety": "Read-only context is for meaning only and must not appear in translation. Preserve all numeric tokens, fractions, units, citations and proper identifiers exactly when possible.",
    }
    # Translation needs a faithful rendered block, not a reasoning trace.
    # Qwen3 and other thinking-capable local models can otherwise spend the
    # whole bounded request window on hidden analysis before producing JSON.
    # This remains an explicit provider setting in the resulting provenance.
    short_heading = _is_short_heading_request(source, document_context)
    if short_heading:
        # Qwen can loop while trying to fill a JSON schema for a one-word
        # heading.  This is still a source-anchored model request, but its
        # contract is deliberately simple and bounded to a heading-sized
        # answer. Context remains read-only evidence in provenance.
        payload = {"model": model, "stream": False, "think": False, "keep_alive": keep_alive,
                   "options": {"temperature": 0, "num_predict": 64},
                   "messages": [{"role": "system", "content": "You are a faithful document translator. Return only the translated heading. No explanation, no JSON, no markdown."},
                                {"role": "user", "content": f"Source language: {source_language}\nTarget language: {target_language}\nOwned heading: {source}\nRead-only context before: {before}\nRead-only context after: {after}"}]}
    else:
        payload = {"model": model, "stream": False, "format": RESPONSE_JSON_SCHEMA, "think": False, "keep_alive": keep_alive,
                   "options": {"temperature": 0, "num_predict": _max_output_tokens_for_source(source)},
                   "messages": [{"role": "system", "content": "You are a faithful document translator."},
                                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]}
    request = urllib.request.Request(endpoint.rstrip("/") + "/api/chat", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise TranslationProviderRequestError(
            f"local Ollama translation request failed: {type(error).__name__}: {error}"
        ) from error
    content = (((result.get("message") or {}).get("content")) if isinstance(result, dict) else None)
    if not isinstance(content, str):
        raise TranslationProviderRequestError("local Ollama response is missing message content")
    content = _strip_model_thinking(content)
    if short_heading:
        extracted = _safe_plain_provider_translation(content, source=source, target_language=target_language)
        if extracted is None:
            diagnostic = re.sub(r"\s+", " ", content).strip()[:160]
            raise TranslationProviderRequestError(f"provider did not return a safe short-heading translation; response_sample={diagnostic!r}")
        return extracted, {"temperature": 0, "max_output_tokens": 64, "thinking": False, "response_format": "plain_short_heading", "prompt_version": PROMPT_VERSION,
                           "glossary_json": glossary_text, "keep_alive": keep_alive,
                           "done_reason": result.get("done_reason"), "eval_count": result.get("eval_count"),
                           "response_format_fallback": False}
    response_format_fallback = False
    try:
        extracted = _extract_translation(content)
    except ValueError as error:
        extracted = _safe_plain_provider_translation(content, source=source, target_language=target_language)
        if extracted is None:
            diagnostic = re.sub(r"\s+", " ", content).strip()[:160]
            raise TranslationProviderRequestError(f"{error}; response_sample={diagnostic!r}") from error
        response_format_fallback = True
    return extracted, {"temperature": 0, "max_output_tokens": _max_output_tokens_for_source(source), "thinking": False, "response_format": "json_schema", "prompt_version": PROMPT_VERSION,
                                           "glossary_json": glossary_text, "keep_alive": keep_alive,
                                           "done_reason": result.get("done_reason"), "eval_count": result.get("eval_count"),
                                           "response_format_fallback": response_format_fallback}


def _eligible(document):
    return [block for block in document["blocks"] if block.get("type") in TEXT_TYPES and isinstance(block.get("text"), str) and block["text"].strip()]


def _document_context(document, *, source_language, target_language, glossary):
    """Stable compact context that never includes neighbouring output units."""
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    return {"document_id": document.get("document_id"), "title": metadata.get("title"),
            "source_language": source_language, "target_language": target_language,
            "glossary_sha256": _digest(glossary),
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


def _valid_partial_translation_checkpoint(root, chunk):
    """Return a verified, ordered prefix from a failed chunk, if present."""
    path = _partial_checkpoint_path(root, chunk["chunk_id"])
    if not path.is_file() or path.is_symlink():
        return None
    payload = _load(path)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        return None
    expected = chunk.get("owned_unit_ids", [])[:len(entries)]
    if [entry.get("block_id") for entry in entries if isinstance(entry, dict)] != expected:
        return None
    if (payload.get("artifact_type") != PARTIAL_CHECKPOINT_TYPE
            or payload.get("chunk_id") != chunk.get("chunk_id")
            or payload.get("entries_sha256") != _digest(entries)):
        return None
    return entries


def _commit_partial_translation_checkpoint(root, chunk, entries):
    """Atomically retain completed owned units before a neighbouring failure."""
    if not entries or [entry.get("block_id") for entry in entries] != chunk["owned_unit_ids"][:len(entries)]:
        raise ValueError("translation partial checkpoint contains units outside chunk ownership")
    payload = {
        "schema_version": CHUNK_SCHEMA_VERSION,
        "artifact_type": PARTIAL_CHECKPOINT_TYPE,
        "chunk_id": chunk["chunk_id"],
        "owned_unit_ids": chunk["owned_unit_ids"][:len(entries)],
        "entries": entries,
        "entries_sha256": _digest(entries),
        "committed_at": _now(),
        "lifecycle": "partial_checkpoint",
    }
    path = _partial_checkpoint_path(root, chunk["chunk_id"])
    _atomic_json(path, payload)
    if _valid_partial_translation_checkpoint(root, chunk) is None:
        raise ValueError("translation partial checkpoint verification failed")


def _commit_translation_checkpoint(root, plan, chunk, entries, *, duration_seconds):
    if [entry.get("block_id") for entry in entries] != chunk["owned_unit_ids"]:
        raise ValueError("translation checkpoint contains units outside chunk ownership")
    for entry in entries:
        if isinstance(entry.get("validation"), dict):
            entry["validation"]["checkpoint"] = "PASS"
    payload = {"schema_version": CHUNK_SCHEMA_VERSION, "artifact_type": CHUNK_CHECKPOINT_TYPE,
               "chunk_id": chunk["chunk_id"], "owned_unit_ids": chunk["owned_unit_ids"], "entries": entries,
               "entries_sha256": _digest(entries), "committed_at": _now(), "duration_seconds": duration_seconds, "lifecycle": "checkpoint"}
    _atomic_json(_checkpoint_path(root, chunk["chunk_id"]), payload)
    if _valid_translation_checkpoint(root, chunk) is None:
        raise ValueError("translation checkpoint verification failed")
    chunk.update({"status": "complete", "completed_at": _now(), "duration_seconds": duration_seconds,
                  "validation": {"status": "PASS", "entries_sha256": payload["entries_sha256"]}})
    partial = _partial_checkpoint_path(root, chunk["chunk_id"])
    if partial.is_file() and not partial.is_symlink():
        partial.unlink()
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


def _entry(block, *, source_language, target_language, text, before, after, glossary, provider, timeout, keep_alive, document_context=None, lineage=None):
    detected = _script_language(text)
    base = {"block_id": block["block_id"], "block_type": block["type"], "source_page": block.get("provenance", {}).get("source_page"),
            "source_text": text, "source_text_sha256": _sha_bytes(text.encode("utf-8")), "detected_language": detected,
            "context": {"previous_block_id": before[0] if before else None, "next_block_id": after[0] if after else None,
                        "before_read_only": before[1] if before else "", "after_read_only": after[1] if after else "",
                        "heading_lineage": lineage or []}}
    # A block already wholly in the target language needs no model call. Mixed
    # blocks are sent with an explicit source language and remain auditable.
    if detected == target_language:
        return {**base, "status": "skipped_already_target_language", "translation": None, "validation": {"numeric_tokens": "not_applicable"}}
    attempts = []
    translated = settings = None
    retry_reason = None
    attempt_limit = MAX_UNIT_ATTEMPTS if len(text) <= MAX_RETRY_SOURCE_CHARS else 1
    for attempt_number in range(1, attempt_limit + 1):
        scoped_context = {**(document_context or {}), "block_type": block["type"], "heading_lineage": lineage or [],
                          "attempt": attempt_number, "retry_reason": retry_reason}
        request_failures = []
        for request_attempt in range(1, MAX_PROVIDER_REQUEST_ATTEMPTS + 1):
            # A loaded local model can need longer than the normal request
            # timeout for an unusually dense unit.  Escalate only retries of
            # that same source-anchored unit, with a documented hard bound.
            request_timeout = min(int(timeout) * request_attempt, MAX_PROVIDER_REQUEST_TIMEOUT_SECONDS)
            try:
                candidate, candidate_settings = _translate_ollama(endpoint=provider["endpoint"], model=provider["model"], source=text,
                    source_language=source_language, target_language=target_language, before=before[1] if before else "", after=after[1] if after else "", glossary=glossary, timeout=request_timeout, keep_alive=keep_alive, document_context=scoped_context)
                break
            except TranslationProviderRequestError as error:
                request_failures.append({"number": request_attempt, "timeout_seconds": request_timeout, "error": str(error)})
                if request_attempt == MAX_PROVIDER_REQUEST_ATTEMPTS:
                    raise
        signals = _response_quality(text, candidate, source_language=source_language, target_language=target_language,
                                    kind=block["type"], glossary=glossary, before=before[1] if before else "", after=after[1] if after else "", response=candidate_settings)
        attempts.append({"number": attempt_number, "translation": candidate, "translation_sha256": _sha_bytes(candidate.encode("utf-8")),
            "provider_settings": candidate_settings, "quality_signals": signals, "retry_reason": retry_reason,
            "attempt_limit": attempt_limit, "provider_request_attempts": request_attempt,
            "provider_request_timeout_seconds": request_timeout, "provider_request_failures": request_failures})
        translated, settings = candidate, candidate_settings
        retry_reason = _retryable_quality_signals(signals)
        if not retry_reason:
            break
    original_numbers, translated_numbers = _numbers(text), _numbers(translated)
    signals = attempts[-1]["quality_signals"]
    status = "machine_translated" if not signals and original_numbers == translated_numbers else "needs_quality_review"
    if original_numbers != translated_numbers:
        status = "needs_numeric_review"
    return {**base, "status": status, "translation": translated, "translation_sha256": _sha_bytes(translated.encode("utf-8")),
            "validation": {"request": "PASS", "structure": "PASS", "checkpoint": "pending", "human_acceptance": "NOT_REVIEWED",
                           "numeric_tokens": "PASS" if original_numbers == translated_numbers else "FAIL", "source": original_numbers, "translation": translated_numbers,
                           "quality_signals": signals, "quality_status": "PASS" if not signals else "REVIEW_REQUIRED"},
            "provider_settings": settings, "attempts": attempts}


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
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS or manifest.get("artifact_type") != ARTIFACT_TYPE:
        raise ValueError("invalid translation schema")
    source = manifest.get("source") or {}
    # The file fingerprint anchors this derivative to the exact CDM artifact
    # used for translation, including any future schema-preserving change.
    stale = cdm_sha is not None and source.get("cdm_sha256") != cdm_sha
    eligible_blocks = _eligible(document)
    selected_ids = source.get("selected_block_ids")
    if selected_ids is None:
        selected_blocks = eligible_blocks
    elif (not isinstance(selected_ids, list) or not selected_ids
          or len(selected_ids) != len(set(selected_ids))):
        raise ValueError("translation has invalid selected logical units")
    else:
        selected_set = set(selected_ids)
        selected_blocks = [block for block in eligible_blocks if block["block_id"] in selected_set]
        if [block["block_id"] for block in selected_blocks] != selected_ids:
            raise ValueError("translation selection is not ordered source logical units")
    expected = {block["block_id"]: block for block in selected_blocks}
    observed = manifest.get("blocks")
    if not isinstance(observed, list) or len(observed) != len(expected) or [item.get("block_id") for item in observed if isinstance(item, dict)] != [block["block_id"] for block in selected_blocks]:
        raise ValueError("translation does not cover the current eligible CDM blocks")
    invalid = []
    quality = []
    for item in observed:
        block = expected[item["block_id"]]
        if item.get("source_text_sha256") != _sha_bytes(block["text"].encode("utf-8")):
            stale = True
        if item.get("status") in {"machine_translated", "needs_numeric_review", "needs_quality_review"} and not isinstance(item.get("translation"), str):
            invalid.append(item["block_id"])
        if isinstance(item.get("translation"), str):
            direction = manifest.get("direction") or {}
            context = item.get("context") or {}
            signals = _response_quality(block["text"], item["translation"], source_language=direction.get("source_language", "en"),
                                        target_language=direction.get("target_language", "ru"), kind=block["type"],
                                        glossary=(manifest.get("validation_policy") or {}).get("glossary", {}),
                                        before=context.get("before_read_only", ""), after=context.get("after_read_only", ""),
                                        response=item.get("provider_settings") or {})
            if signals: quality.append({"block_id": block["block_id"], "source_page": block.get("provenance", {}).get("source_page"), "signals": signals})
    return {"valid": not invalid, "stale": stale, "invalid_block_ids": invalid,
            "machine_translated": sum(item.get("status") == "machine_translated" for item in observed),
            "needs_numeric_review": sum(item.get("status") == "needs_numeric_review" for item in observed),
            "needs_quality_review": sum(item.get("status") == "needs_quality_review" for item in observed),
            "skipped": sum(str(item.get("status", "")).startswith("skipped_") for item in observed),
            "quality_policy": "signals are human-review evidence, not automatic corrections", "quality_review": quality, "quality_review_count": len(quality),
            "semantic_acceptance": "HUMAN_REVIEW_REQUIRED"}


def _reset_translation_workspace(root):
    """Discard only a verified BookText-owned translation checkpoint workspace."""
    workspace = _translation_workspace(root).resolve()
    marker = _load(_workspace_marker(root))
    if workspace.exists():
        if not marker or marker.get("artifact_type") != CHUNK_WORKSPACE_TYPE or marker.get("path") != str(workspace):
            raise ValueError("refusing to reset unauthenticated translation checkpoint workspace")
        shutil.rmtree(workspace)


def translate_document(output_dir, *, source_language, target_language, provider, model, glossary_path=None, endpoint="http://127.0.0.1:11434", timeout=120, keep_alive=DEFAULT_MODEL_KEEP_ALIVE, chunk_size=DEFAULT_LOGICAL_UNIT_CHUNK_SIZE, restart=False, progress=None, block_ids=None):
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
    execution_keep_alive = _effective_request_keep_alive(keep_alive, timeout)
    metadata = {**_model_info(endpoint, model, timeout), "resource_lifecycle": {
        "requested_idle_keep_alive": keep_alive,
        "execution_keep_alive": execution_keep_alive,
        "policy": "BookText requests a bounded Ollama model lease for active work; it never stops the shared daemon or unloads a model by process name.",
    }}
    all_blocks = _eligible(document)
    if block_ids is None:
        blocks = all_blocks
        selected_block_ids = None
    else:
        if not isinstance(block_ids, (list, tuple)) or not block_ids or any(not isinstance(value, str) for value in block_ids):
            raise ValueError("block_ids must be a non-empty list of logical-unit IDs")
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block_ids must not contain duplicates")
        requested = set(block_ids)
        blocks = [block for block in all_blocks if block["block_id"] in requested]
        selected_block_ids = [block["block_id"] for block in blocks]
        if len(selected_block_ids) != len(block_ids):
            raise ValueError("block_ids contain an ineligible or unknown CDM block")
    translation_id = "translation-" + _sha_bytes((document.get("document_id", "") + pair + cdm_sha).encode("utf-8"))[:20]
    options = {"direction": {"source_language": source_language, "target_language": target_language}, "provider": "ollama", "model": model,
               "model_digest": metadata.get("model_digest"), "glossary": glossary_provenance, "keep_alive": keep_alive,
               "prompt_version": PROMPT_VERSION, "quality_policy_version": QUALITY_POLICY_VERSION, "context_char_budget": CONTEXT_CHAR_BUDGET,
               "max_output_tokens": MAX_OUTPUT_TOKENS, "max_retry_source_chars": MAX_RETRY_SOURCE_CHARS,
               "selected_block_ids": selected_block_ids}
    units = [_unit_spec(block) for block in blocks]
    source_fingerprint = _digest({"cdm_sha256": cdm_sha, "units": units, "options": options})
    destination = translation_path(root, source_language, target_language)
    existing = _load(destination)
    expected_reuse = {"model": model, "model_digest": metadata.get("model_digest"), "glossary": glossary_provenance,
                      "prompt_version": PROMPT_VERSION, "quality_policy_version": QUALITY_POLICY_VERSION, "keep_alive": keep_alive,
                      "max_output_tokens": MAX_OUTPUT_TOKENS, "max_retry_source_chars": MAX_RETRY_SOURCE_CHARS}
    if not restart and existing and (existing.get("source") or {}).get("cdm_sha256") == cdm_sha and all((existing.get("generation") or {}).get(key) == value for key, value in expected_reuse.items()):
        validation = _validate_manifest(existing, document, cdm_sha=cdm_sha)
        if validation["valid"] and not validation["stale"]:
            return {"path": str(destination), "reused": True, "validation": validation}
    active_plan = _load(_plan_path(root))
    compatible_active_plan = bool(active_plan) and all(active_plan.get(key) == value for key, value in {
        "translation_id": translation_id, "source_fingerprint": source_fingerprint,
        "options_fingerprint": _digest(options),
    }.items())
    if existing:
        history = destination.parent / "history" / f"{pair}-{(existing.get('source') or {}).get('cdm_sha256', 'unknown')[:12]}.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        if not history.exists(): _atomic_json(history, existing)
    # A previous completed derivative may be stale while a newer matching plan
    # is in progress. Preserve that plan and its partial unit checkpoints; only
    # reset an authenticated workspace when the plan itself is incompatible or
    # the user explicitly requests a restart.
    if restart or (existing and not compatible_active_plan):
        _reset_translation_workspace(root)
    plan, plan_created = _create_or_load_translation_plan(root, translation_id=translation_id, source_fingerprint=source_fingerprint,
        units=units, options=options, chunk_size=chunk_size)
    if progress:
        progress("chunk_plan_created" if plan_created else "job_resumed", {"chunks": len(plan["chunks"]), "logical_units": len(units), "translation_id": translation_id})
    by_id = {block["block_id"]: block for block in blocks}
    # A bounded pilot may own a subset, while its read-only meaning context
    # remains anchored in the complete ordered CDM.
    index_by_id = {block["block_id"]: index for index, block in enumerate(all_blocks)}
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
        started = time.monotonic()
        partial = _valid_partial_translation_checkpoint(root, chunk)
        entries = list(partial or [])
        if partial and progress:
            progress("chunk_progress", {"chunk_id": chunk["chunk_id"], "status": "partial_resume",
                                        "current": len(entries), "total": len(chunk["owned_unit_ids"]),
                                        "logical_units": len(entries)})
        try:
            for block_id in chunk["owned_unit_ids"][len(entries):]:
                block = by_id[block_id]; index = index_by_id[block_id]
                before = (all_blocks[index - 1]["block_id"], _sentences(all_blocks[index - 1]["text"], tail=True)) if index else None
                after = (all_blocks[index + 1]["block_id"], _sentences(all_blocks[index + 1]["text"])) if index + 1 < len(all_blocks) else None
                entries.append(_entry(block, source_language=source_language, target_language=target_language, text=block["text"], before=before, after=after,
                    glossary=glossary, provider=metadata, timeout=timeout, keep_alive=execution_keep_alive, document_context=context, lineage=_heading_lineage(all_blocks, index)))
                _commit_partial_translation_checkpoint(root, chunk, entries)
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
        "source": {"document_id": document.get("document_id"), "cdm_path": "book_document.json", "cdm_sha256": cdm_sha, "raw_ocr_mutated": False,
                   "decision_provenance": document.get("review", {}), "selected_block_ids": selected_block_ids},
        "provider": metadata, "glossary": glossary_provenance,
        "generation": expected_reuse, "validation_policy": {"version": QUALITY_POLICY_VERSION, "glossary": glossary,
                                                                "semantic_acceptance": "HUMAN_REVIEW_REQUIRED"}, "blocks": entries,
        "execution": {"logical_unit_ownership": True, "document_context": context, "checkpoint_plan": str(_plan_path(root).relative_to(root)),
                      "source_fingerprint": source_fingerprint, "chunk_count": len(plan["chunks"]), "resumed_chunks": resumed_chunks,
                      "assembly": {"coverage": "PASS", "ordering": "PASS", "duplicates": "PASS", "context_only_output": "NOT_SEMANTICALLY_VERIFIED"}},
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
