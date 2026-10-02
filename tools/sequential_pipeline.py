"""Durable, strictly sequential BookText pipeline definitions.

This module owns *only* the plan and its atomic stage state.  OCR, review,
final-book, CDM, translation and export remain implemented by their existing
production modules and are invoked by ``machine_protocol``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from tools import review_decisions


SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "booktext.sequential_pipeline"
FILENAME = ".booktext_pipeline.json"
STAGE_TYPES = ("PROCESS", "REVIEW_POLICY", "FINAL_BUILD", "CDM", "TRANSLATE", "EXPORT")
TERMINAL = {"COMPLETE", "SKIPPED"}


class PipelineError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def path(root):
    return Path(root).expanduser().resolve() / FILENAME


def _atomic(target, value):
    review_decisions.atomic_write_json(target, value)


def _load(root):
    target = path(root)
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineError("pipeline state is unreadable") from error
    if not isinstance(value, dict) or value.get("artifact_type") != ARTIFACT_TYPE or value.get("schema_version") != SCHEMA_VERSION:
        raise PipelineError("pipeline state has an unsupported schema")
    return value


def _pipeline_id(root):
    return "pipeline-" + hashlib.sha256(str(Path(root).expanduser().resolve()).encode("utf-8")).hexdigest()[:20]


def default_definition(*, review_policy="review_when_required", translation=None, export_formats=None, processing_args=None):
    """Return the small supported workflow surface; this is not a DAG API."""
    stages = [
        {"stage_id": "process", "type": "PROCESS", "config": {"core_args": list(processing_args or [])}},
        {"stage_id": "review", "type": "REVIEW_POLICY", "config": {"policy": review_policy}},
        {"stage_id": "final-build", "type": "FINAL_BUILD", "config": {}},
        {"stage_id": "cdm", "type": "CDM", "config": {}},
    ]
    if translation:
        stages.append({"stage_id": "translate", "type": "TRANSLATE", "config": dict(translation)})
    if export_formats:
        stages.append({"stage_id": "export", "type": "EXPORT", "config": {"formats": list(export_formats), "document": "TRANSLATION" if translation else "ORIGINAL"}})
    return {"schema_version": SCHEMA_VERSION, "stages": stages}


def _stage_ids(stages):
    return [stage.get("stage_id") for stage in stages if isinstance(stage, dict)]


def validate_definition(definition):
    if not isinstance(definition, dict):
        raise PipelineError("pipeline definition must be an object")
    stages = definition.get("stages")
    if not isinstance(stages, list) or not stages:
        raise PipelineError("pipeline definition requires ordered stages")
    if _stage_ids(stages) != list(dict.fromkeys(_stage_ids(stages))) or any(not isinstance(item, str) or not item for item in _stage_ids(stages)):
        raise PipelineError("pipeline stage IDs must be non-empty and unique")
    kinds = []
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("type") not in STAGE_TYPES or not isinstance(stage.get("config", {}), dict):
            raise PipelineError("pipeline has an unsupported stage")
        kinds.append(stage["type"])
    if kinds[0] != "PROCESS":
        raise PipelineError("pipeline must begin with PROCESS")
    if "REVIEW_POLICY" not in kinds or kinds.index("REVIEW_POLICY") != 1:
        raise PipelineError("REVIEW_POLICY must immediately follow PROCESS")
    required = ["PROCESS", "REVIEW_POLICY", "FINAL_BUILD", "CDM"]
    if any(kind not in kinds for kind in required) or not (kinds.index("FINAL_BUILD") < kinds.index("CDM")):
        raise PipelineError("pipeline requires FINAL_BUILD followed by CDM")
    if len(set(kinds)) != len(kinds):
        raise PipelineError("each supported pipeline stage may appear once")
    if "TRANSLATE" in kinds:
        translation_index = kinds.index("TRANSLATE")
        if translation_index <= kinds.index("CDM"):
            raise PipelineError("TRANSLATE requires completed CDM")
        config = stages[translation_index]["config"]
        if config.get("source_language") not in {"en", "ru"} or config.get("target_language") not in {"en", "ru"} or config.get("source_language") == config.get("target_language"):
            raise PipelineError("TRANSLATE requires distinct source and target languages")
        if not isinstance(config.get("model"), str) or not config["model"].strip():
            raise PipelineError("TRANSLATE requires a local model")
    if "EXPORT" in kinds:
        export_index = kinds.index("EXPORT")
        if export_index != len(kinds) - 1:
            raise PipelineError("EXPORT must be the final pipeline stage")
        config = stages[export_index]["config"]
        formats = config.get("formats")
        if not isinstance(formats, list) or not formats or any(not isinstance(item, str) or not item for item in formats):
            raise PipelineError("EXPORT requires one or more formats")
        document = config.get("document", "ORIGINAL")
        if document not in {"ORIGINAL", "TRANSLATION"}:
            raise PipelineError("EXPORT document must be ORIGINAL or TRANSLATION")
        if document == "TRANSLATION" and "TRANSLATE" not in kinds:
            raise PipelineError("translated export requires TRANSLATE")
    policy = stages[kinds.index("REVIEW_POLICY")]["config"].get("policy")
    if policy not in {"review_when_required", "skip_human_review"}:
        raise PipelineError("REVIEW_POLICY must be review_when_required or skip_human_review")
    return {"valid": True, "stage_types": kinds}


def create(root, *, source, definition):
    root = Path(root).expanduser().resolve()
    validation = validate_definition(definition)
    existing = _load(root)
    if existing:
        if existing.get("definition") != definition:
            raise PipelineError("a different pipeline already belongs to this project")
        return existing, False
    root.mkdir(parents=True, exist_ok=True)
    now = _now()
    state = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "pipeline_id": _pipeline_id(root),
        "project_id": None,
        "source": str(Path(source).expanduser().resolve()),
        "definition": definition,
        "validation": validation,
        "status": "PLANNED",
        "current_stage_id": None,
        "completed_stage_ids": [],
        "stages": [{**stage, "status": "PENDING", "attempts": 0, "artifact_refs": {}, "validation": None} for stage in definition["stages"]],
        "failure": None,
        "cancellation": None,
        "created_at": now,
        "updated_at": now,
    }
    _atomic(path(root), state)
    return state, True


def save(root, state):
    state["updated_at"] = _now()
    _atomic(path(root), state)
    return state


def stage(state, stage_id):
    value = next((item for item in state.get("stages", []) if item.get("stage_id") == stage_id), None)
    if value is None:
        raise PipelineError("pipeline stage is missing")
    return value


def next_pending(state):
    return next((item for item in state.get("stages", []) if item.get("status") not in TERMINAL), None)


def mark_running(root, state, item):
    item.update({"status": "RUNNING", "attempts": int(item.get("attempts", 0)) + 1, "started_at": _now(), "failure": None})
    state.update({"status": "RUNNING", "current_stage_id": item["stage_id"], "failure": None, "cancellation": None})
    return save(root, state)


def mark_complete(root, state, item, *, validation, artifact_refs):
    item.update({"status": "COMPLETE", "completed_at": _now(), "validation": validation, "artifact_refs": artifact_refs, "failure": None})
    if item["stage_id"] not in state["completed_stage_ids"]:
        state["completed_stage_ids"].append(item["stage_id"])
    state["current_stage_id"] = None
    return save(root, state)


def mark_validating(root, state, item):
    item.update({"status": "VALIDATING", "validating_at": _now()})
    state["current_stage_id"] = item["stage_id"]
    return save(root, state)


def mark_committing(root, state, item, *, validation, artifact_refs):
    item.update({"status": "COMMITTING", "committing_at": _now(), "validation": validation, "artifact_refs": artifact_refs})
    state["current_stage_id"] = item["stage_id"]
    return save(root, state)


def mark_paused(root, state, item, *, code, message, cancellation=False):
    item.update({"status": "PAUSED", "paused_at": _now(), "failure": {"code": code, "message": message}})
    state.update({"status": "PAUSED", "current_stage_id": item["stage_id"], "failure": None,
                  "cancellation": {"code": code, "message": message, "at": _now()} if cancellation else {"code": code, "message": message, "at": _now()}})
    return save(root, state)


def mark_failed(root, state, item, *, code, message, details=None):
    failure = {"code": code, "message": message, "details": details or {}, "at": _now()}
    item.update({"status": "FAILED", "failed_at": _now(), "failure": failure})
    state.update({"status": "FAILED", "current_stage_id": item["stage_id"], "failure": failure})
    return save(root, state)


def complete_pipeline(root, state):
    state.update({"status": "COMPLETED", "current_stage_id": None, "completed_at": _now(), "failure": None, "cancellation": None})
    return save(root, state)
