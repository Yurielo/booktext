"""Persisted, ownership-safe chunk plans for bounded BookText processing.

This module deliberately knows nothing about OCR, QA, or translation semantics.
It plans and checkpoints *execution* units while callers retain ownership of
canonical document units.  A chunk may read adjacent context in a future
operation, but only its ``owned_pages`` may be committed by that operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "1.0"
WORKSPACE_NAME = "chunks"
MARKER = ".booktext-chunk-workspace.json"
PLAN_FILENAME = "chunk_plan.json"
LIFECYCLE = ("transient", "checkpoint", "evidence", "canonical")


class ChunkPlanError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(path):
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise ChunkPlanError(f"invalid chunk artifact: {Path(path).name}: {error}") from error
    if not isinstance(result, dict):
        raise ChunkPlanError(f"invalid chunk artifact: {Path(path).name}")
    return result


def source_fingerprint(sources):
    """Content fingerprint, intentionally independent of absolute paths."""
    hasher = hashlib.sha256()
    for source in sorted((Path(value) for value in sources), key=lambda value: value.name):
        hasher.update(source.name.encode("utf-8")); hasher.update(b"\0")
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
    return hasher.hexdigest()


def auto_chunk_size(page_count, *, operation="ocr", source_type=None, complexity=0, previous_duration=None):
    """Small deterministic policy; callers may later provide richer evidence.

    Eight pages is the normal starting bound.  Image/table-heavy evidence and
    an expensive previous chunk downshift it; native embedded text can use ten.
    """
    if page_count <= 1:
        return max(page_count, 1)
    if source_type in {"native_text", "pdf_embedded_text"} and not complexity:
        size = 10
    else:
        size = 8
    if complexity >= 3 or (previous_duration is not None and previous_duration > 120):
        size = 5
    return max(1, min(size, page_count))


def _workspace(root):
    return Path(root).expanduser().resolve() / WORKSPACE_NAME


def _ensure_workspace(root, *, job_id, source_fp):
    workspace = _workspace(root)
    workspace.mkdir(parents=True, exist_ok=True)
    marker_path = workspace / MARKER
    marker = _load(marker_path)
    if marker is None:
        marker = {"schema_version": SCHEMA_VERSION, "artifact_type": "booktext.chunk_workspace", "workspace_id": str(uuid.uuid4()), "job_id": job_id, "source_fingerprint": source_fp, "path": str(workspace), "created_at": _now()}
        _atomic_json(marker_path, marker)
    if marker.get("artifact_type") != "booktext.chunk_workspace" or marker.get("path") != str(workspace):
        raise ChunkPlanError("chunk workspace ownership marker is invalid")
    return workspace


def _chunk(chunk_id, pages, total_pages, *, context_pages=1):
    owned = list(pages)
    before = [page for page in range(max(1, owned[0] - context_pages), owned[0])]
    after = [page for page in range(owned[-1] + 1, min(total_pages, owned[-1] + context_pages) + 1)]
    return {"chunk_id": chunk_id, "owned_pages": owned, "owned_range": [owned[0], owned[-1]],
            "context_before_pages": before, "context_after_pages": after,
            "context_range": before + owned + after, "status": "pending", "attempts": 0,
            "artifacts": {"manifest": f"{WORKSPACE_NAME}/{chunk_id}/manifest.json", "result": f"{WORKSPACE_NAME}/{chunk_id}/result.json"},
            "validation": None, "started_at": None, "completed_at": None, "duration_seconds": None}


def create_or_load_plan(root, *, job_id, source_fp, operation, page_numbers, options=None, chunk_size=None, source_type=None, complexity=0):
    """Create one deterministic plan or fail closed if its source changed."""
    root = Path(root).expanduser().resolve()
    page_numbers = list(page_numbers)
    if page_numbers != sorted(set(page_numbers)) or not page_numbers:
        raise ChunkPlanError("chunk plan requires a non-empty ordered unique page list")
    options = options or {}
    option_fp = _digest(options)
    workspace = _ensure_workspace(root, job_id=job_id, source_fp=source_fp)
    path = workspace / PLAN_FILENAME
    existing = _load(path)
    if existing is not None:
        expected = {"source_fingerprint": source_fp, "operation": operation, "options_fingerprint": option_fp, "page_numbers": page_numbers}
        if any(existing.get(key) != value for key, value in expected.items()):
            existing["status"] = "stale"
            existing["stale_reason"] = "source_or_operation_changed"
            _atomic_json(path, existing)
            raise ChunkPlanError("existing chunk plan is stale; source or operation options changed")
        return existing
    size = int(chunk_size) if chunk_size is not None else auto_chunk_size(len(page_numbers), operation=operation, source_type=source_type, complexity=complexity)
    if not 1 <= size <= 100:
        raise ChunkPlanError("chunk size must be between 1 and 100 pages")
    chunks = [_chunk(f"chunk-{index:04d}", page_numbers[offset:offset + size], max(page_numbers)) for index, offset in enumerate(range(0, len(page_numbers), size), start=1)]
    plan = {"schema_version": SCHEMA_VERSION, "artifact_type": "booktext.chunk_plan", "job_id": job_id,
            "source_fingerprint": source_fp, "operation": operation, "options": options,
            "options_fingerprint": option_fp, "page_numbers": page_numbers, "total_pages": len(page_numbers),
            "chunk_size": size, "mode": "explicit" if chunk_size is not None else "AUTO", "status": "active",
            "chunks": chunks, "created_at": _now(), "updated_at": _now(), "assembly": {"status": "pending"}}
    _atomic_json(path, plan)
    return plan


def plan_path(root):
    return _workspace(root) / PLAN_FILENAME


def save_plan(root, plan):
    plan["updated_at"] = _now()
    _atomic_json(plan_path(root), plan)


def chunk_dir(root, chunk):
    workspace = _workspace(root)
    target = (workspace / chunk["chunk_id"]).resolve()
    if target.parent != workspace.resolve() or target.is_symlink():
        raise ChunkPlanError("unsafe chunk path")
    target.mkdir(parents=True, exist_ok=True)
    return target


def result_fingerprint(payload):
    return _digest(payload)


def valid_checkpoint(root, chunk):
    """Return validated result payload or ``None``; never trust status alone."""
    result = Path(root).resolve() / chunk["artifacts"]["result"]
    manifest = Path(root).resolve() / chunk["artifacts"]["manifest"]
    if not result.is_file() or not manifest.is_file() or result.is_symlink() or manifest.is_symlink():
        return None
    try:
        payload, meta = _load(result), _load(manifest)
    except ChunkPlanError:
        return None
    expected = {f"page-{page:04d}" for page in chunk["owned_pages"]}
    if set(payload) != expected or meta.get("status") != "complete" or meta.get("result_sha256") != result_fingerprint(payload):
        return None
    return payload


def valid_compacted_checkpoint(canonical_payload, chunk):
    """Validate a compacted checkpoint against retained canonical raw evidence."""
    if chunk.get("status") != "complete" or not chunk.get("artifacts", {}).get("result_compacted"):
        return None
    expected = {f"page-{page:04d}" for page in chunk.get("owned_pages", [])}
    payload = {key: canonical_payload[key] for key in expected if key in canonical_payload}
    digest = (chunk.get("validation") or {}).get("result_sha256")
    if set(payload) != expected or not digest or result_fingerprint(payload) != digest:
        return None
    return payload


def begin_attempt(root, plan, chunk):
    chunk["status"] = "running"; chunk["attempts"] = int(chunk.get("attempts", 0)) + 1; chunk["started_at"] = _now()
    save_plan(root, plan)
    return chunk_dir(root, chunk)


def commit_checkpoint(root, plan, chunk, payload, *, duration_seconds, diagnostics=None):
    expected = {f"page-{page:04d}" for page in chunk["owned_pages"]}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ChunkPlanError("chunk result is incomplete or contains pages outside its ownership")
    directory = chunk_dir(root, chunk)
    digest = result_fingerprint(payload)
    _atomic_json(directory / "result.json", payload)
    manifest = {"schema_version": SCHEMA_VERSION, "artifact_type": "booktext.chunk_checkpoint", "chunk_id": chunk["chunk_id"],
                "owned_pages": chunk["owned_pages"], "context_pages": chunk["context_range"], "status": "complete",
                "result_sha256": digest, "duration_seconds": duration_seconds, "diagnostics": diagnostics or {}, "lifecycle": "checkpoint", "committed_at": _now()}
    _atomic_json(directory / "manifest.json", manifest)
    # Verify before marking plan complete.  Cleanup must only occur afterwards.
    if valid_checkpoint(root, chunk) is None:
        raise ChunkPlanError("chunk checkpoint verification failed")
    chunk.update({"status": "complete", "validation": {"status": "PASS", "result_sha256": digest}, "completed_at": _now(), "duration_seconds": duration_seconds})
    save_plan(root, plan)
    return manifest


def fail_attempt(root, plan, chunk, error):
    chunk.update({"status": "failed", "last_error": str(error), "failed_at": _now()})
    save_plan(root, plan)


def mark_assembly(root, plan, *, complete, validation=None):
    plan["assembly"] = {"status": "complete" if complete else "failed", "validation": validation or {}, "completed_at": _now()}
    plan["status"] = "complete" if complete else "active"
    save_plan(root, plan)


def _safe_child(workspace, relative):
    target = (workspace / relative).resolve()
    return target if target.is_relative_to(workspace.resolve()) and not target.is_symlink() else None


def compact(root, *, require_assembly=True):
    """Delete only regenerable checkpoint payloads after a verified assembly.

    The plan and per-chunk manifests are retained as compact provenance.  The
    caller must preserve an assembled canonical raw result separately.
    """
    root = Path(root).expanduser().resolve(); workspace = _workspace(root); plan = _load(workspace / PLAN_FILENAME)
    marker = _load(workspace / MARKER)
    if not plan or not marker or marker.get("path") != str(workspace):
        raise ChunkPlanError("refusing cleanup outside an authenticated chunk workspace")
    if require_assembly and plan.get("assembly", {}).get("status") != "complete":
        return {"cleanup_allowed": False, "reason": "final_assembly_not_verified", "bytes_before": 0, "bytes_deleted": 0, "bytes_remaining": _workspace_size(workspace), "files_deleted": 0, "retained_categories": ["checkpoint", "evidence", "canonical"]}
    before = _workspace_size(workspace); deleted = files = 0
    for chunk in plan.get("chunks", []):
        relative = Path(chunk.get("artifacts", {}).get("result", ""))
        target = _safe_child(workspace, relative.relative_to(WORKSPACE_NAME) if relative.parts and relative.parts[0] == WORKSPACE_NAME else relative)
        if target and target.is_file():
            size = target.stat().st_size; target.unlink(); deleted += size; files += 1
            chunk.setdefault("artifacts", {})["result_compacted"] = True
    plan["compaction"] = {"completed_at": _now(), "bytes_before": before, "bytes_deleted": deleted, "files_deleted": files, "retained_categories": ["evidence", "canonical", "compact_checkpoint_metadata"]}
    save_plan(root, plan)
    return {"cleanup_allowed": True, "bytes_before": before, "bytes_deleted": deleted, "bytes_remaining": _workspace_size(workspace), "files_deleted": files, "retained_categories": plan["compaction"]["retained_categories"]}


def _workspace_size(path):
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file() and not item.is_symlink()) if Path(path).exists() else 0


def disk_budget(root, *, estimated_transient_bytes):
    usage = shutil.disk_usage(Path(root).expanduser().resolve())
    reserve = 256 * 1024 * 1024
    return {"available_bytes": usage.free, "estimated_transient_bytes": estimated_transient_bytes, "reserve_bytes": reserve,
            "ok": usage.free >= estimated_transient_bytes + reserve}
