"""Provenance-preserving, opt-in EN↔RU translation derived from BookText CDM.

This module never edits OCR, review decisions, final-book files, or the CDM.
It invokes a caller-selected local provider only when a translation command is
explicitly requested.  The resulting artifact is source anchored and can be
validated for staleness without contacting the provider again.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from tools import canonical_document, review_decisions


SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "booktext.translation"
SUPPORTED_LANGUAGES = ("en", "ru")
TEXT_TYPES = {"heading", "paragraph", "list", "caption", "unknown"}
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


def pair_name(source_language, target_language):
    if source_language not in SUPPORTED_LANGUAGES or target_language not in SUPPORTED_LANGUAGES or source_language == target_language:
        raise ValueError("translation direction must be en→ru or ru→en")
    return f"{source_language}-{target_language}"


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


def _translate_ollama(*, endpoint, model, source, source_language, target_language, before, after, glossary, timeout):
    glossary_text = json.dumps(glossary, ensure_ascii=False, sort_keys=True) if glossary else "{}"
    prompt = {
        "task": "Translate only the source block. Do not correct, summarize, omit, add commentary, or translate numbers/identifiers.",
        "source_language": source_language,
        "target_language": target_language,
        "source_block": source,
        "previous_context": before,
        "next_context": after,
        "required_glossary": glossary,
        "response": "Return exactly JSON: {\"translation\": \"...\"}",
        "safety": "Preserve all numeric tokens, fractions, units, citations and proper identifiers exactly when possible.",
    }
    payload = {"model": model, "stream": False, "format": "json", "options": {"temperature": 0},
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
    return _extract_translation(content), {"temperature": 0, "response_format": "json", "glossary_json": glossary_text}


def _eligible(document):
    return [block for block in document["blocks"] if block.get("type") in TEXT_TYPES and isinstance(block.get("text"), str) and block["text"].strip()]


def _numbers(text):
    return NUMBER_RE.findall(text)


def _entry(block, *, source_language, target_language, text, before, after, glossary, provider, timeout):
    detected = _script_language(text)
    base = {"block_id": block["block_id"], "block_type": block["type"], "source_page": block.get("provenance", {}).get("source_page"),
            "source_text": text, "source_text_sha256": _sha_bytes(text.encode("utf-8")), "detected_language": detected,
            "context": {"previous_block_id": before[0] if before else None, "next_block_id": after[0] if after else None}}
    # A block already wholly in the target language needs no model call. Mixed
    # blocks are sent with an explicit source language and remain auditable.
    if detected == target_language:
        return {**base, "status": "skipped_already_target_language", "translation": None, "validation": {"numeric_tokens": "not_applicable"}}
    translated, settings = _translate_ollama(endpoint=provider["endpoint"], model=provider["model"], source=text,
        source_language=source_language, target_language=target_language, before=before[1] if before else "", after=after[1] if after else "", glossary=glossary, timeout=timeout)
    original_numbers, translated_numbers = _numbers(text), _numbers(translated)
    status = "machine_translated" if original_numbers == translated_numbers else "needs_numeric_review"
    return {**base, "status": status, "translation": translated, "translation_sha256": _sha_bytes(translated.encode("utf-8")),
            "validation": {"numeric_tokens": "PASS" if status == "machine_translated" else "FAIL", "source": original_numbers, "translation": translated_numbers},
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
    if not isinstance(observed, list) or {item.get("block_id") for item in observed if isinstance(item, dict)} != set(expected):
        raise ValueError("translation does not cover the current eligible CDM blocks")
    invalid = []
    for item in observed:
        block = expected[item["block_id"]]
        if item.get("source_text_sha256") != _sha_bytes(block["text"].encode("utf-8")):
            stale = True
        if item.get("status") in {"machine_translated", "needs_numeric_review"} and not isinstance(item.get("translation"), str):
            invalid.append(item["block_id"])
    return {"valid": not invalid, "stale": stale, "invalid_block_ids": invalid,
            "machine_translated": sum(item.get("status") == "machine_translated" for item in observed),
            "needs_numeric_review": sum(item.get("status") == "needs_numeric_review" for item in observed),
            "skipped": sum(str(item.get("status", "")).startswith("skipped_") for item in observed)}


def translate_document(output_dir, *, source_language, target_language, provider, model, glossary_path=None, endpoint="http://127.0.0.1:11434", timeout=120):
    root = Path(output_dir).expanduser().resolve()
    pair = pair_name(source_language, target_language)
    if provider != "ollama":
        raise ValueError("only explicit local Ollama translation is currently supported")
    document = _load(root / "book_document.json")
    decisions = _load(root / "review_decisions.json", {})
    canonical_document.validate_document(document, root=root, decisions=decisions)
    glossary, glossary_provenance = _glossary(glossary_path)
    cdm_sha = sha_file(root / "book_document.json")
    destination = translation_path(root, source_language, target_language)
    existing = _load(destination)
    if existing and (existing.get("source") or {}).get("cdm_sha256") == cdm_sha and (existing.get("provider") or {}).get("model") == model and existing.get("glossary") == glossary_provenance:
        validation = _validate_manifest(existing, document, cdm_sha=cdm_sha)
        if validation["valid"] and not validation["stale"]:
            return {"path": str(destination), "reused": True, "validation": validation}
    if existing:
        history = destination.parent / "history" / f"{pair}-{(existing.get('source') or {}).get('cdm_sha256', 'unknown')[:12]}.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        if not history.exists(): _atomic_json(history, existing)
    metadata = _model_info(endpoint, model, timeout)
    blocks = _eligible(document)
    entries = []
    for index, block in enumerate(blocks):
        before = (blocks[index - 1]["block_id"], blocks[index - 1]["text"][-700:]) if index else None
        after = (blocks[index + 1]["block_id"], blocks[index + 1]["text"][:700]) if index + 1 < len(blocks) else None
        entries.append(_entry(block, source_language=source_language, target_language=target_language, text=block["text"], before=before, after=after, glossary=glossary, provider=metadata, timeout=timeout))
    destination.parent.mkdir(parents=True, exist_ok=True)
    target_markdown = destination.parent / f"book-{pair}.md"
    bilingual_markdown = destination.parent / f"book-{pair}-bilingual.md"
    target_markdown.write_text(_render(entries), encoding="utf-8")
    bilingual_markdown.write_text(_render(entries, bilingual=True), encoding="utf-8")
    manifest = {"schema_version": SCHEMA_VERSION, "artifact_type": ARTIFACT_TYPE, "translation_id": "translation-" + _sha_bytes((document.get("document_id", "") + pair + cdm_sha).encode("utf-8"))[:20],
        "created_at": _now(), "direction": {"source_language": source_language, "target_language": target_language},
        "source": {"document_id": document.get("document_id"), "cdm_path": "book_document.json", "cdm_sha256": cdm_sha, "raw_ocr_mutated": False, "decision_provenance": document.get("review", {})},
        "provider": metadata, "glossary": glossary_provenance, "blocks": entries,
        "outputs": {"translation_markdown": str(target_markdown.relative_to(root)), "bilingual_markdown": str(bilingual_markdown.relative_to(root))},
        "policy": "Derived translation only. Raw OCR, review decisions, final-book artifacts, and CDM remain unchanged. Machine translation is not human-reviewed automatically."}
    manifest["validation"] = _validate_manifest(manifest, document, cdm_sha=cdm_sha)
    _atomic_json(destination, manifest)
    return {"path": str(destination), "reused": False, "validation": manifest["validation"], "outputs": manifest["outputs"]}


def validate_translation(output_dir, *, source_language, target_language):
    root = Path(output_dir).expanduser().resolve()
    manifest = _load(translation_path(root, source_language, target_language))
    if manifest is None:
        raise ValueError("translation artifact does not exist")
    document = _load(root / "book_document.json")
    result = _validate_manifest(manifest, document, cdm_sha=sha_file(root / "book_document.json"))
    return {"path": str(translation_path(root, source_language, target_language)), **result}
