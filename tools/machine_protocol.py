"""Versioned JSONL contract for a native BookText application.

This module is intentionally an orchestration layer.  It calls the existing
production functions and never implements OCR, review decisions, CDM, or
export semantics a second time.  In machine mode stdout is *only* JSONL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tools import canonical_document, chunk_lifecycle, export_document, project_history, review_decisions, review_workspace, review_recovery, sequential_pipeline, translation
from tools.process_lifecycle import ManagedProcess


PROTOCOL_VERSION = "1.1"
JOB_SCHEMA_VERSION = "1.0"
JOB_FILENAME = ".booktext_job.json"
PHASES = ("preflight", "prepare", "ocr", "qa", "visuals/tables", "review", "final_build", "cdm", "translation", "export")
EVENT_TYPES = ("job_started", "phase_started", "progress", "warning", "artifact_created", "review_summary", "phase_completed", "job_completed", "job_failed", "job_cancelled", "chunk_plan_created", "chunk_started", "chunk_progress", "chunk_completed", "chunk_failed", "chunk_retry", "job_resumed", "assembly_started", "assembly_completed", "cleanup_started", "cleanup_progress", "cleanup_completed", "cleanup_warning", "pipeline_started", "stage_started", "stage_progress", "stage_completed", "stage_failed", "pipeline_paused", "pipeline_resumed", "pipeline_completed")
EXIT_CODES = {"success": 0, "invalid_input": 2, "preflight_failure": 3, "processing_failure": 4, "validation_failure": 5, "cancelled": 130, "stale_conflict": 6}
COMMAND_PHASE = {"run": "ocr", "cleanup": "prepare", "recover": "review", "decision": "review", "review": "review", "review-item": "review", "review-clusters": "review", "batch-preview": "review", "batch-confirm": "review", "batch-revert": "review", "baseline-preview": "review", "baseline-confirm": "review", "baseline-revert": "review", "final-build": "final_build", "cdm": "cdm", "document-preview": "cdm", "translation": "translation", "translation-validate": "translation", "translation-export": "export", "export": "export", "pipeline-create": "prepare", "pipeline-validate": "prepare", "pipeline-start": "prepare", "pipeline-status": None, "pipeline-resume": "prepare", "pipeline-cancel": "prepare", "inspect": None, "project-history": None, "project-remove": None}


class MachineError(RuntimeError):
    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic(path, value):
    review_decisions.atomic_write_json(path, value)


def _load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as error:
        raise MachineError("validation_failure", f"invalid JSON artifact: {Path(path).name}", details={"error": str(error)}) from error


class Emitter:
    def __init__(self, job_id): self.job_id = job_id

    def emit(self, event_type, *, phase=None, current=None, total=None, page=None, code="ok", message="", details=None):
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event_type}")
        if phase is not None and phase not in PHASES:
            raise ValueError(f"unsupported phase: {phase}")
        event = {"protocol_version": PROTOCOL_VERSION, "job_id": self.job_id, "event_type": event_type,
                 "timestamp": _now(), "phase": phase, "current": current, "total": total, "page": page,
                 "code": code, "message": message, "details": details or {}}
        print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)
        return event


def _job_path(root): return Path(root).expanduser().resolve() / JOB_FILENAME


def _job_id(root):
    value = str(Path(root).expanduser().resolve()).encode("utf-8")
    return "job-" + hashlib.sha256(value).hexdigest()[:20]


def ensure_job(root, *, status="created", source=None):
    root = Path(root).expanduser().resolve(); root.mkdir(parents=True, exist_ok=True)
    path = _job_path(root); state = _load(path)
    if not isinstance(state, dict):
        state = {"schema_version": JOB_SCHEMA_VERSION, "artifact_type": "booktext.machine_job", "protocol_version": PROTOCOL_VERSION,
                 "job_id": _job_id(root), "output_dir": str(root), "source": source or [], "status": status,
                 "last_phase": None, "created_at": _now(), "updated_at": _now(), "artifacts": {}}
    state.setdefault("job_id", _job_id(root)); state.setdefault("artifacts", {}); state["updated_at"] = _now()
    _atomic(path, state); return state


def read_job(root):
    """Read a durable job if present, without creating or changing anything.

    Inspection/history is intentionally different from a run: a stale output
    path must never gain a `.booktext_job.json` merely because it appeared in a
    recent-project list.
    """
    root = Path(root).expanduser().resolve()
    state = _load(_job_path(root))
    if not isinstance(state, dict):
        return {"schema_version": JOB_SCHEMA_VERSION, "artifact_type": "booktext.machine_job",
                "protocol_version": PROTOCOL_VERSION, "job_id": _job_id(root), "output_dir": str(root),
                "source": [], "status": "discovered", "last_phase": None, "artifacts": {}}
    state = dict(state)
    state.setdefault("job_id", _job_id(root)); state.setdefault("output_dir", str(root)); state.setdefault("source", [])
    state.setdefault("artifacts", {}); state.setdefault("status", "discovered"); state.setdefault("last_phase", None)
    return state


def update_job(root, **changes):
    state = ensure_job(root)
    # A retry that reaches a verified terminal success must not inherit an
    # earlier failure's UI state.  Artifacts/history remain intact; only the
    # mutually exclusive terminal diagnostics are cleared.
    if changes.get("status") == "completed":
        for key in ("error_code", "error_details", "exit_code"):
            state.pop(key, None)
        changes.setdefault("partial", False)
    state.update(changes); state["updated_at"] = _now(); _atomic(_job_path(root), state); return state


def artifact_map(root):
    root = Path(root).expanduser().resolve()
    candidates = {
        "run_summary": "run_summary.json", "raw_review": "review.json", "review": "review_priority.json",
        "review_text": "review_priority.txt", "clusters": "review_clusters.json", "decisions": "review_decisions.json",
        "final_build": "final_build.json", "final_text": "book_final.txt", "final_markdown": "book_final.md",
        "cdm": "book_document.json", "exports": "exports/export_manifest.json", "book_manifest": "book.json",
        "clean_text": "book_clean.txt", "markdown": "book.md", "document_identity": "document_identity.json", "deliverables": "deliverables", "page_images": "pages", "images": "images", "descriptions": "descriptions",
        "translations": "translations",
        "extraction_report": "extraction/report.json", "source_inventory": "extraction/inventory.json",
    }
    result = {name: {"path": value, "exists": (root / value).exists()} for name, value in candidates.items()}
    # A translated derivative is intentionally namespaced by language pair.
    # Expose it through the same stable logical artifact name as original
    # exports so native Project History never has to hard-code filenames.
    if not result["exports"]["exists"]:
        translated = sorted((root / "translations" / "exports").glob("*/export_manifest.json"))
        if translated:
            result["exports"] = {"path": str(translated[0].relative_to(root)), "exists": True}
    return result


def _has_booktext_evidence(artifacts, state):
    """Whether this directory can be safely treated as a BookText output."""
    if state.get("status") not in {None, "discovered", "created"}:
        return True
    return any(artifacts[name]["exists"] for name in (
        "book_manifest", "run_summary", "raw_review", "review", "final_build", "cdm", "exports", "document_identity", "translations",
    ))


def _icloud_managed_path(root):
    """Avoid blocking the native launch on a dataless iCloud placeholder."""
    normalized = str(Path(root).expanduser())
    return "/Library/Mobile Documents/" in normalized or "/Library/CloudStorage/iCloud Drive/" in normalized


def _unavailable_inspection(root, *, detail):
    root = Path(root).expanduser().resolve()
    state = {"schema_version": JOB_SCHEMA_VERSION, "artifact_type": "booktext.machine_job",
             "protocol_version": PROTOCOL_VERSION, "job_id": _job_id(root), "output_dir": str(root),
             "source": [], "status": "discovered", "last_phase": None, "artifacts": {}}
    unavailable = {"state": "FAILED", "action": "Locate project files", "availability": "PROJECT_FILES_UNAVAILABLE",
                   "page_count": None, "translation_state": "NOT_STARTED", "last_phase": None,
                   "source_unavailable": False, "reopen_available": False, "detail": detail}
    return {"protocol_version": PROTOCOL_VERSION, "job": {**state, "complete": False, "incomplete": True},
            "artifacts": {}, "review": {"unresolved_priority_counts": {"HIGH": 0, "MEDIUM": 0}, "stale_or_conflict_count": 0},
            "cdm": {"present": False, "schema_version": None}, "exports": {"present": False, "validation": None},
            "run_summary": None, "project": unavailable}


def _translation_state(root, artifacts):
    plan = _load(Path(root) / "translations" / "chunks" / "translation_plan.json", {}) or {}
    manifest = _load(Path(root) / "translations" / "en-ru.json", {}) or {}
    chunks = plan.get("chunks") if isinstance(plan, dict) else None
    if isinstance(chunks, list) and chunks and any(chunk.get("status") != "complete" for chunk in chunks if isinstance(chunk, dict)):
        return "RESUMABLE" if any(chunk.get("status") == "complete" for chunk in chunks if isinstance(chunk, dict)) else "IN_PROGRESS"
    if isinstance(manifest, dict) and manifest.get("artifact_type") == translation.ARTIFACT_TYPE:
        validation = manifest.get("validation") or {}
        return "READY" if validation.get("valid", True) and not validation.get("stale", False) else "FAILED"
    return "NOT_STARTED"


def _project_projection(root, state, artifacts, counts, summary, final, document, export, pipeline=None):
    """Classify only already-recorded job/artifact truth; never start work."""
    sources = state.get("source") if isinstance(state.get("source"), list) else []
    source_unavailable = bool(sources) and any(not Path(value).exists() for value in sources if isinstance(value, str))
    translation_state = _translation_state(root, artifacts)
    job_status, phase = state.get("status"), state.get("last_phase")
    # Existing results remain usable when their original source has moved.
    # The source is required only for resume/processing, not for reopening a
    # completed book, review queue, final build, or already-created translation.
    availability = "SOURCE_UNAVAILABLE" if source_unavailable else "AVAILABLE"
    if isinstance(pipeline, dict) and pipeline.get("status") == "PLANNED":
        current, action = "PIPELINE_PLANNED", "Start pipeline"
    elif isinstance(pipeline, dict) and pipeline.get("status") == "RUNNING":
        current, action, availability = "PIPELINE_RUNNING", "Resume pipeline", "AVAILABLE"
    elif isinstance(pipeline, dict) and pipeline.get("status") == "PAUSED":
        current, action = "PIPELINE_PAUSED", "Resume pipeline"
    elif isinstance(pipeline, dict) and pipeline.get("status") == "FAILED":
        current, action = "FAILED", "Retry pipeline"
    elif job_status == "running":
        current, action, availability = ("TRANSLATING", "Resume Translation", "AVAILABLE") if phase == "translation" else ("PROCESSING", "Resume Processing", "AVAILABLE")
    elif job_status == "cancelled":
        if source_unavailable:
            current, action = "FAILED", "Locate source"
        else:
            current, action = "INTERRUPTED_RESUMABLE", "Resume Translation" if phase == "translation" else "Resume Processing"
    elif job_status == "failed":
        current, action = "FAILED", "Locate source" if source_unavailable else ("Resume Translation" if phase == "translation" else "Resume Processing")
    elif translation_state == "IN_PROGRESS":
        current, action, availability = "TRANSLATING", "Resume Translation", "AVAILABLE"
    elif translation_state == "RESUMABLE":
        current, action = ("FAILED", "Locate source") if source_unavailable else ("INTERRUPTED_RESUMABLE", "Resume Translation")
    elif counts.get("HIGH", 0) or counts.get("MEDIUM", 0):
        current, action = "REVIEW_REQUIRED", "Review items"
    elif translation_state == "READY":
        current, action = "TRANSLATION_READY", "Open translation"
    elif not artifacts["final_build"]["exists"]:
        current, action = "READY_FOR_FINAL_BUILD", "Create final book"
    elif artifacts["exports"]["exists"]:
        current, action = "COMPLETED", "Open book"
    else:
        current, action = "BOOK_READY", "Open book"
    pages = (summary.get("pages") or {}).get("processed") if isinstance(summary.get("pages"), dict) else None
    if not isinstance(pages, int):
        pages = len((_load(Path(root) / "book.json", {}) or {}).get("pages", []))
    projection = {"state": current, "action": action, "availability": availability, "page_count": pages or None,
            "translation_state": translation_state, "last_phase": phase, "source_unavailable": source_unavailable,
            "reopen_available": True}
    if isinstance(pipeline, dict):
        projection["pipeline_status"] = pipeline.get("status")
        projection["pipeline_current_stage"] = pipeline.get("current_stage_id")
        projection["pipeline_completed_stages"] = list(pipeline.get("completed_stage_ids") or [])
    return projection


def inspect_output(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir(): raise MachineError("invalid_input", "output directory does not exist", details={"output_dir": str(root)})
    if _icloud_managed_path(root):
        return _unavailable_inspection(root, detail="iCloud-managed project files must be available locally before reopening")
    state = read_job(root)
    artifacts = artifact_map(root)
    if not _has_booktext_evidence(artifacts, state):
        return _unavailable_inspection(root, detail="BookText project evidence is unavailable")
    # Publication exports of translations live under their language pair.  Do
    # not make callers (including the native app) guess that implementation
    # detail: artifact_map() is the single discovery authority.
    export_path = artifacts.get("exports", {}).get("path")
    summary, final, document = (_load(root / name, {}) or {} for name in ("run_summary.json", "final_build.json", "book_document.json"))
    export = _load(root / export_path, {}) if export_path else {}
    export = export or {}
    # Job records written by older Core versions can retain an obsolete static
    # export path.  Inspection is deliberately non-mutating, but its returned
    # artifact view must always reflect the current on-disk project.
    state = dict(state or {})
    state["artifacts"] = artifacts
    priority = _load(root / "review_priority.json", {}) or {}
    counts = (final.get("unresolved_priority_counts") or document.get("review", {}).get("unresolved_priority_counts")
              or priority.get("counts") or {})
    stale = final.get("stale_or_conflict_count", 0)
    # A pre-protocol output may not retain run_summary.json, but a validated
    # CDM/export is still a completed recoverable BookText result.
    complete = state.get("status") == "completed" or (
        (bool(summary) or artifacts["cdm"]["exists"] or artifacts["exports"]["exists"])
        and state.get("status") not in {"running", "cancelled", "failed"}
    )
    try:
        pipeline = sequential_pipeline._load(root)
    except sequential_pipeline.PipelineError as error:
        raise MachineError("validation_failure", str(error)) from error
    project = _project_projection(root, state, artifacts, counts, summary, final, document, export, pipeline=pipeline)
    return {"protocol_version": PROTOCOL_VERSION, "job": {**state, "complete": complete, "incomplete": not complete},
            "artifacts": artifacts, "review": {"unresolved_priority_counts": {"HIGH": counts.get("HIGH", 0), "MEDIUM": counts.get("MEDIUM", 0)}, "stale_or_conflict_count": stale},
            "cdm": {"present": artifacts["cdm"]["exists"], "schema_version": document.get("schema_version")},
            "exports": {"present": artifacts["exports"]["exists"], "validation": export.get("validation")},
            "run_summary": summary.get("pages"), "pipeline": pipeline, "project": project}


def _table_preview(root, block):
    """Return bounded verified cells for presentation, never reconstructed data."""
    if block.get("table_status") != "structured":
        return None
    json_path = (block.get("artifacts") or {}).get("json")
    if not isinstance(json_path, str):
        return None
    payload = _load(Path(root) / json_path)
    grid = payload.get("grid") if isinstance(payload, dict) else None
    if not isinstance(grid, list) or not all(isinstance(row, list) for row in grid):
        return None
    # This is only a bounded projection of the already verified table artifact.
    return [[str(cell) for cell in row[:12]] for row in grid[:30]]


def document_preview(root, *, index=0, limit=25):
    """Return a bounded slice of an existing CDM for native UI navigation.

    It is deliberately read-only: no OCR, final build, CDM rebuild, or model
    is started merely to inspect a book.
    """
    root = Path(root).expanduser().resolve()
    document = _load(root / "book_document.json")
    if not isinstance(document, dict):
        raise MachineError("invalid_input", "canonical document has not been built", details={"path": "book_document.json"})
    try:
        canonical_document.validate_document(document, root=root, decisions=_load(root / "review_decisions.json", {}))
    except ValueError as error:
        raise MachineError("validation_failure", str(error)) from error
    blocks = document["blocks"]
    index, limit = min(max(index, 0), len(blocks)), min(max(limit, 1), 100)
    public_keys = ("block_id", "type", "text", "table_status", "review_required", "image", "description", "artifacts", "rows", "columns", "reading_order", "provenance", "boundary_kind", "gap", "normalization_warnings")
    counts = {kind: sum(block.get("type") == kind for block in blocks) for kind in canonical_document.BLOCK_TYPES}
    public_blocks = []
    for block in blocks[index:index + limit]:
        value = {key: block.get(key) for key in public_keys}
        table = _table_preview(root, block)
        if table is not None:
            value["table_preview"] = table
        public_blocks.append(value)
    return {"document_id": document.get("document_id"), "metadata": document.get("metadata", {}), "review": document.get("review", {}), "warnings": document.get("warnings", []), "block_counts": counts, "count": len(blocks), "index": index, "limit": limit, "blocks": public_blocks}


def _dependency(name, *, path=None):
    value = str(path) if path else shutil.which(name)
    return {"name": name, "available": bool(value and Path(value).exists() if path else value), "path": value}


def preflight(operation="status"):
    """Only report dependencies relevant to the selected operation."""
    from booktext import SURYA_CLI, VISION_OCR, __version__
    required = []
    optional = []
    if operation in {"run", "status"}:
        required = []  # Actual source routing validates only the tools it uses.
        optional = [_dependency("surya_ocr", path=SURYA_CLI), _dependency("llama-server"), _dependency("visionocr", path=VISION_OCR), _dependency("tesseract"), _dependency("ocrmypdf")]
    elif operation == "export":
        required = [] ; optional = [_dependency("pandoc"), _dependency("pdfinfo")]
    elif operation == "native-text":
        required = [_dependency("pdftotext"), _dependency("pdfimages"), _dependency("pdfinfo")]
    elif operation == "translation":
        # Translation is opt-in and must never make local Ollama a requirement
        # for OCR, review, CDM, or export.
        optional = [_dependency("ollama")]
    result = {"protocol_version": PROTOCOL_VERSION, "booktext_version": __version__, "operation": operation,
              "dependencies": {"required": required, "optional": optional},
              "features": {"run": True, "surya": bool(optional and optional[0]["available"] and optional[1]["available"]) if operation in {"run", "status"} else None,
                           "vision_qa": bool(len(optional) > 2 and optional[2]["available"]) if operation in {"run", "status"} else None,
                           "native_pdf_text": operation == "native-text" and all(item["available"] for item in required),
                           "review": True, "final_build": True, "cdm": True, "translation": operation == "translation", "export": True},
              "errors": [{"code": "missing_required_dependency", "dependency": item["name"], "message": f"Missing required dependency: {item['name']}"} for item in required if not item["available"]]}
    result["ok"] = not result["errors"]
    return result


def processing_preflight(sources, core_args):
    """Validate only dependencies selected by the actual processing argv."""
    from booktext import SURYA_CLI, VISION_OCR
    options = list(core_args)
    def selected(name, default=None):
        try: return options[options.index(name) + 1]
        except (ValueError, IndexError): return default
    if "--native-text-only" in options:
        source = Path(sources[0])
        required = [_dependency("pdftotext"), _dependency("pdfimages"), _dependency("pdfinfo")]
        if source.suffix.lower() != ".pdf":
            required.append({"name": "PDF input", "available": False, "path": None})
        missing = [item for item in required if not item["available"]]
        return {"required": required, "missing": missing, "ocr": "native_pdf_text", "qa_enabled": False, "pdf_enabled": False}
    ocr, no_qa, no_pdf = selected("--ocr", "auto"), "--no-qa" in options, "--no-pdf" in options
    if ocr == "auto" and "--searchable-pdf" not in options: no_pdf = True
    if ocr == "auto" and Path(sources[0]).suffix.lower() == ".pdf":
        required = [_dependency(name) for name in ("pdftotext", "pdfimages", "pdftoppm", "pdfinfo", "mutool")]
        if not no_pdf: required.extend([_dependency("tesseract"), _dependency("ocrmypdf")])
        return {"required": required, "missing": [item for item in required if not item["available"]],
                "ocr": "auto", "qa_enabled": not no_qa, "pdf_enabled": not no_pdf,
                "deferred_dependencies": "Surya/Vision required only if source analysis finds OCR regions"}
    if ocr == "auto": ocr = "surya"
    required = [_dependency("magick")]
    if ocr == "surya": required.extend([_dependency("surya_ocr", path=SURYA_CLI), _dependency("llama-server")])
    if ocr == "surya" and not no_qa: required.append(_dependency("visionocr", path=VISION_OCR))
    if ocr == "tesseract" or not no_pdf: required.append(_dependency("tesseract"))
    if not no_pdf: required.append(_dependency("ocrmypdf"))
    suffix = Path(sources[0]).suffix.lower()
    if suffix == ".pdf": required.append(_dependency("pdftoppm"))
    if suffix in {".djvu", ".djv"}: required.extend([_dependency("ddjvu"), _dependency("djvused")])
    missing = [item for item in required if not item["available"]]
    return {"required": required, "missing": missing, "ocr": ocr, "qa_enabled": ocr == "surya" and not no_qa, "pdf_enabled": not no_pdf}


def _invoke(emitter, root, phase, operation, fn):
    state = ensure_job(root, status="running")
    emitter.emit("job_started", phase=phase, message=f"Starting {operation}", details={"output_dir": str(Path(root).resolve())})
    update_job(root, status="running", last_phase=phase)
    emitter.emit("phase_started", phase=phase, message=f"{operation} started")
    result = fn()
    artifacts = artifact_map(root); completed_state = update_job(root, status="completed", last_phase=phase, artifacts=artifacts)
    project_history.record(root, completed_state)
    for name, value in artifacts.items():
        if value["exists"] and name in {"final_build", "cdm", "exports", "decisions"}:
            emitter.emit("artifact_created", phase=phase, message=f"Artifact available: {name}", details={"name": name, **value})
    emitter.emit("phase_completed", phase=phase, message=f"{operation} completed")
    emitter.emit("job_completed", phase=phase, message=f"{operation} completed", details={"result": result, "artifacts": artifacts})
    return EXIT_CODES["success"]


def _run_existing(emitter, sources, core_args, *, complete_job=True):
    if not sources: raise MachineError("invalid_input", "at least one source is required")
    source = Path(sources[0]).expanduser().resolve()
    if not source.exists(): raise MachineError("invalid_input", "source does not exist", details={"source": str(source)})
    from tools.automatic_extraction import output_directory
    def option(name, default=None):
        try: return core_args[core_args.index(name)+1]
        except (ValueError, IndexError): return default
    root = output_directory(source, automatic=option("--ocr", "auto") == "auto" and "--native-text-only" not in core_args, explicit=option("--output-dir"))
    state = ensure_job(root, status="running", source=[str(Path(value).expanduser().resolve()) for value in sources]); emitter.job_id = state["job_id"]
    project_history.record(root, state)
    emitter.emit("job_started", phase="preflight", message="Processing job started", details={"source": state["source"], "output_dir": str(root)})
    emitter.emit("phase_started", phase="preflight", message="Checking processing prerequisites")
    pf = processing_preflight(sources, core_args)
    if pf["missing"]:
        update_job(root, status="failed", last_phase="preflight", error_code="preflight_failure", error_details=pf)
        raise MachineError("preflight_failure", "required processing dependency is missing", details=pf)
    emitter.emit("phase_completed", phase="preflight", message="Preflight completed")
    update_job(root, status="running", last_phase="prepare", launcher_pid=os.getpid())
    emitter.emit("phase_started", phase="prepare", message="Preparing source pages")
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "booktext.py"), *sources, *core_args, "--machine-events"]
    child = ManagedProcess(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    try:
        for line in iter(child.process.stdout.readline, ""):
            line = line.rstrip()
            if line.startswith("BOOKTEXT_CHUNK_EVENT "):
                try:
                    chunk_event = json.loads(line.removeprefix("BOOKTEXT_CHUNK_EVENT "))
                    kind, details = chunk_event["kind"], chunk_event.get("details", {})
                    event_type = kind if kind in EVENT_TYPES else "warning"
                    pages = details.get("pages") if isinstance(details, dict) else None
                    page = pages[0] if isinstance(pages, list) and pages else None
                    emitter.emit(event_type, phase=details.get("phase", "ocr" if kind != "cleanup_completed" else "prepare"), current=details.get("current") if isinstance(details, dict) else None, total=details.get("total") if isinstance(details, dict) else None, page=page, code=kind, message=kind.replace("_", " "), details=details)
                    continue
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    emitter.emit("warning", phase="ocr", code="invalid_chunk_event", message="Ignoring invalid Core chunk event")
            if line: print(line, file=sys.stderr, flush=True)
            match = __import__("re").search(r"страниц: (\d+) из (\d+)|страницы (\d+)/(\d+)", line)
            if match:
                current, total = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
                emitter.emit("progress", phase="prepare", current=int(current), total=int(total), page=int(current), message="Page preparation progress")
        code = child.process.wait()
    except KeyboardInterrupt:
        child.cleanup(); update_job(root, status="cancelled", last_phase="ocr", partial=True)
        emitter.emit("job_cancelled", phase="ocr", code="cancelled", message="Processing cancelled; owned child tree cleaned", details={"output_dir": str(root)})
        return EXIT_CODES["cancelled"]
    finally:
        child.cleanup()
    if code:
        update_job(root, status="failed", last_phase="ocr", partial=True, exit_code=code)
        raise MachineError("processing_failure", "processing core exited unsuccessfully", details={"exit_code": code})
    completed_state = update_job(root, status="completed" if complete_job else "running", last_phase="review", partial=False, artifacts=artifact_map(root))
    project_history.record(root, completed_state)
    emitter.emit("phase_completed", phase="review", message="Processing and review completed")
    summary = _load(root / "run_summary.json", {}) or {}
    for warning in summary.get("warnings", []):
        emitter.emit("warning", phase="review", code=warning.get("kind", "warning"), message=warning.get("message", "Processing warning"), details=warning)
    emitter.emit("review_summary", phase="review", message="Review summary available", details=summary.get("review_priority", {}))
    for name, value in artifact_map(root).items():
        if value["exists"]: emitter.emit("artifact_created", phase="review", message=f"Artifact available: {name}", details={"name": name, **value})
    if complete_job:
        emitter.emit("job_completed", phase="review", message="Processing completed", details={"output_dir": str(root)})
    return EXIT_CODES["success"]


def _pipeline_phase(item):
    return {"PROCESS": "ocr", "REVIEW_POLICY": "review", "FINAL_BUILD": "final_build", "CDM": "cdm", "TRANSLATE": "translation", "EXPORT": "export"}[item["type"]]


def _pipeline_refs(root, names):
    artifacts = artifact_map(root)
    refs = {name: artifacts[name] for name in names if name in artifacts and artifacts[name]["exists"]}
    # Translation exports are deliberately namespaced by language pair rather
    # than stored in the original-document exports directory.  The pipeline's
    # logical EXPORT artifact is valid in either production location.
    if "exports" in names and "exports" not in refs:
        translated = sorted((Path(root) / "translations" / "exports").glob("*/export_manifest.json"))
        if translated:
            manifest = translated[0]
            refs["exports"] = {"path": str(manifest.relative_to(root)), "exists": True}
    return refs


def _final_build(root):
    book, priority = review_decisions.load_json(Path(root) / "book.json"), review_decisions.load_json(Path(root) / "review_priority.json")
    decisions_path = Path(root) / "review_decisions.json"
    if decisions_path.exists():
        decisions = review_decisions.load_json(decisions_path)
    else:
        decisions = review_decisions.empty_decisions(Path(root) / "review_priority.json", Path(root) / "book.json")
        review_decisions.atomic_write_json(decisions_path, decisions)
    text, markdown, report = review_decisions.build_final(book, priority, decisions, book_path=Path(root) / "book.json", priority_path=Path(root) / "review_priority.json", decisions_path=decisions_path, booktext_version="0.1.0")
    review_decisions.write_final(root, text, markdown, report)
    if report["stale_or_conflict_count"]:
        raise MachineError("stale_conflict", "final build has stale or conflicting decisions", details=report)
    return report


def _parse_pipeline_definition(value):
    try:
        definition = json.loads(value)
    except json.JSONDecodeError as error:
        raise MachineError("invalid_input", "pipeline definition must be JSON", details={"error": str(error)}) from error
    try:
        return definition, sequential_pipeline.validate_definition(definition)
    except sequential_pipeline.PipelineError as error:
        raise MachineError("invalid_input", str(error)) from error


def _pipeline_root(source, core_args, explicit=None):
    from tools.automatic_extraction import output_directory
    source = Path(source).expanduser().resolve()
    if explicit:
        return Path(explicit).expanduser().resolve()
    def option(name, default=None):
        try:
            return core_args[core_args.index(name) + 1]
        except (ValueError, IndexError):
            return default
    return output_directory(source, automatic=option("--ocr", "auto") == "auto" and "--native-text-only" not in core_args, explicit=option("--output-dir"))


def _pipeline_create(source, definition_json, explicit_root=None):
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise MachineError("invalid_input", "source does not exist", details={"source": str(source_path)})
    definition, validation = _parse_pipeline_definition(definition_json)
    process = next(item for item in definition["stages"] if item["type"] == "PROCESS")
    core_args = process["config"].get("core_args", [])
    if not isinstance(core_args, list) or any(not isinstance(item, str) for item in core_args):
        raise MachineError("invalid_input", "PROCESS core_args must be an array of strings")
    root = _pipeline_root(source_path, core_args, explicit_root)
    try:
        state, created = sequential_pipeline.create(root, source=source_path, definition=definition)
    except sequential_pipeline.PipelineError as error:
        raise MachineError("validation_failure", str(error)) from error
    job = ensure_job(root, status="planned", source=[str(source_path)])
    state["project_id"] = job["job_id"]
    sequential_pipeline.save(root, state)
    project_history.record(root, job)
    return root, state, {"created": created, **validation}


def _validate_completed_pipeline_stage(root, item):
    refs = item.get("artifact_refs") or {}
    if not refs:
        return False
    for reference in refs.values():
        relative = reference.get("path") if isinstance(reference, dict) else None
        if not isinstance(relative, str) or not (Path(root) / relative).exists():
            return False
    return True


def _run_pipeline(root, *, resume=False):
    root = Path(root).expanduser().resolve()
    try:
        state = sequential_pipeline._load(root)
    except sequential_pipeline.PipelineError as error:
        raise MachineError("validation_failure", str(error)) from error
    if not state:
        raise MachineError("invalid_input", "pipeline does not exist for this project")
    try:
        sequential_pipeline.validate_definition(state.get("definition"))
    except sequential_pipeline.PipelineError as error:
        raise MachineError("validation_failure", str(error)) from error
    job = ensure_job(root, status="running", source=[state["source"]])
    state["project_id"] = job["job_id"]
    sequential_pipeline.save(root, state)
    emitter = Emitter(job["job_id"])
    emitter.emit("pipeline_resumed" if resume else "pipeline_started", phase="prepare", message="Pipeline resumed" if resume else "Pipeline started", details={"pipeline_id": state["pipeline_id"], "output_dir": str(root), "stages": [item["type"] for item in state["stages"]], "source": state["source"]})
    project_history.record(root, job)
    for item in state["stages"]:
        if item.get("status") == "COMPLETE":
            if _validate_completed_pipeline_stage(root, item):
                continue
            item.update({"status": "PENDING", "validation": {"valid": False, "reason": "required artifact missing"}})
            state["completed_stage_ids"] = [identifier for identifier in state.get("completed_stage_ids", []) if identifier != item["stage_id"]]
            sequential_pipeline.save(root, state)
        if item.get("status") == "PAUSED" or item.get("status") == "FAILED":
            item["status"] = "PENDING"
            sequential_pipeline.save(root, state)
        phase = _pipeline_phase(item)
        sequential_pipeline.mark_running(root, state, item)
        update_job(root, status="running", last_phase=phase, pipeline_id=state["pipeline_id"])
        emitter.emit("stage_started", phase=phase, message=f"{item['type'].replace('_', ' ').title()} started", details={"pipeline_id": state["pipeline_id"], "stage_id": item["stage_id"], "stage_type": item["type"]})
        try:
            config = item["config"]
            if item["type"] == "PROCESS":
                result = _run_existing(emitter, [state["source"]], config.get("core_args", []), complete_job=False)
                if result == EXIT_CODES["cancelled"]:
                    sequential_pipeline.mark_paused(root, state, item, code="cancelled", message="Processing cancelled at a safe checkpoint", cancellation=True)
                    update_job(root, status="cancelled", last_phase=phase, partial=True, pipeline_id=state["pipeline_id"])
                    emitter.emit("pipeline_paused", phase=phase, code="cancelled", message="Pipeline paused; completed work is retained", details={"pipeline_id": state["pipeline_id"], "stage_id": item["stage_id"]})
                    return EXIT_CODES["cancelled"]
                validation, refs = {"valid": True, "book_manifest": "PASS"}, _pipeline_refs(root, ("book_manifest", "run_summary", "raw_review", "review", "clusters"))
            elif item["type"] == "REVIEW_POLICY":
                priority = _load(root / "review_priority.json", {}) or {}
                counts = priority.get("counts", {}) if isinstance(priority, dict) else {}
                unresolved = int(counts.get("HIGH", 0) or 0) + int(counts.get("MEDIUM", 0) or 0)
                if config["policy"] == "review_when_required" and unresolved:
                    sequential_pipeline.mark_paused(root, state, item, code="REVIEW_REQUIRED", message=f"{unresolved} HIGH/MEDIUM review items require a human decision")
                    update_job(root, status="paused", last_phase=phase, pipeline_id=state["pipeline_id"])
                    emitter.emit("pipeline_paused", phase=phase, code="REVIEW_REQUIRED", message="Pipeline paused for required review", details={"pipeline_id": state["pipeline_id"], "unresolved": unresolved, "policy": config["policy"]})
                    return EXIT_CODES["success"]
                validation, refs = {"valid": True, "policy": config["policy"], "unresolved_high_medium": unresolved, "raw_evidence_mutated": False}, _pipeline_refs(root, ("review", "raw_review", "decisions"))
            elif item["type"] == "FINAL_BUILD":
                result = _final_build(root)
                validation, refs = {"valid": True, **result}, _pipeline_refs(root, ("final_build", "final_text", "final_markdown", "decisions"))
            elif item["type"] == "CDM":
                document_path = canonical_document.write_document(canonical_document.build_document(root), root)
                validation, refs = {"valid": True, "path": str(document_path.relative_to(root))}, _pipeline_refs(root, ("cdm",))
            elif item["type"] == "TRANSLATE":
                def translation_progress(event_type, details):
                    emitter.emit("stage_progress", phase="translation", current=details.get("current"), total=details.get("total"), code=event_type, message=event_type.replace("_", " "), details={"pipeline_id": state["pipeline_id"], **details})
                result = translation.translate_document(root, source_language=config["source_language"], target_language=config["target_language"], provider=config.get("provider", "ollama"), model=config["model"], glossary_path=config.get("glossary"), endpoint=config.get("endpoint", "http://127.0.0.1:11434"), timeout=config.get("timeout", 120), keep_alive=config.get("keep_alive", translation.DEFAULT_MODEL_KEEP_ALIVE), chunk_size=config.get("chunk_size", translation.DEFAULT_LOGICAL_UNIT_CHUNK_SIZE), progress=translation_progress)
                validation = result.get("validation") or {}
                if not validation.get("valid", False) or validation.get("stale", False):
                    raise MachineError("validation_failure", "translation validation did not pass", details=validation)
                refs = _pipeline_refs(root, ("translations",))
            elif item["type"] == "EXPORT":
                formats = config["formats"]
                if config.get("document") == "TRANSLATION":
                    translation_stage = next(value for value in state["stages"] if value["type"] == "TRANSLATE")
                    translation_config = translation_stage["config"]
                    result = export_document.export_translation(root, formats, source_language=translation_config["source_language"], target_language=translation_config["target_language"])
                else:
                    result = export_document.export_document(root, formats)
                validation, refs = {"valid": True, "formats": formats, "result": result}, _pipeline_refs(root, ("exports", "deliverables"))
            else:
                raise MachineError("validation_failure", "unsupported pipeline stage")
        except KeyboardInterrupt:
            sequential_pipeline.mark_paused(root, state, item, code="cancelled", message="Pipeline cancelled at a safe boundary", cancellation=True)
            update_job(root, status="cancelled", last_phase=phase, partial=True, pipeline_id=state["pipeline_id"])
            emitter.emit("pipeline_paused", phase=phase, code="cancelled", message="Pipeline paused; completed work is retained", details={"pipeline_id": state["pipeline_id"], "stage_id": item["stage_id"]})
            return EXIT_CODES["cancelled"]
        except (MachineError, ValueError, OSError, subprocess.SubprocessError) as error:
            code = error.code if isinstance(error, MachineError) else "stage_failure"
            details = error.details if isinstance(error, MachineError) else {"type": type(error).__name__}
            sequential_pipeline.mark_failed(root, state, item, code=code, message=str(error), details=details)
            update_job(root, status="failed", last_phase=phase, partial=True, pipeline_id=state["pipeline_id"])
            emitter.emit("stage_failed", phase=phase, code=code, message=str(error), details={"pipeline_id": state["pipeline_id"], "stage_id": item["stage_id"], **details})
            emitter.emit("job_failed", phase=phase, code=code, message=str(error), details={"pipeline_id": state["pipeline_id"], "stage_id": item["stage_id"]})
            return EXIT_CODES.get(code, EXIT_CODES["validation_failure"])
        required_refs = {
            "PROCESS": ("book_manifest",), "FINAL_BUILD": ("final_build", "final_text"),
            "CDM": ("cdm",), "TRANSLATE": ("translations",), "EXPORT": ("exports",),
        }.get(item["type"], ())
        missing_refs = [name for name in required_refs if name not in refs]
        if missing_refs:
            validation = {**validation, "valid": False, "missing_artifacts": missing_refs}
        # Persist the final two boundaries separately.  If the host quits here,
        # resume verifies the recorded artifacts instead of assuming success.
        sequential_pipeline.mark_validating(root, state, item)
        if not validation.get("valid", False):
            # Validation happens after the stage's producer has returned.  It
            # must still follow the same fail-closed pipeline transition as an
            # execution error; otherwise a relaunch could misrepresent an
            # invalid stage as merely in-progress.
            message = "pipeline stage validation did not pass"
            sequential_pipeline.mark_failed(root, state, item, code="validation_failure", message=message, details=validation)
            update_job(root, status="failed", last_phase=phase, partial=True, pipeline_id=state["pipeline_id"])
            emitter.emit("stage_failed", phase=phase, code="validation_failure", message=message, details={"pipeline_id": state["pipeline_id"], "stage_id": item["stage_id"], **validation})
            emitter.emit("job_failed", phase=phase, code="validation_failure", message=message, details={"pipeline_id": state["pipeline_id"], "stage_id": item["stage_id"]})
            return EXIT_CODES["validation_failure"]
        sequential_pipeline.mark_committing(root, state, item, validation=validation, artifact_refs=refs)
        sequential_pipeline.mark_complete(root, state, item, validation=validation, artifact_refs=refs)
        emitter.emit("stage_completed", phase=phase, message=f"{item['type'].replace('_', ' ').title()} completed", details={"pipeline_id": state["pipeline_id"], "stage_id": item["stage_id"], "validation": validation, "artifacts": refs})
    sequential_pipeline.complete_pipeline(root, state)
    completed = update_job(root, status="completed", last_phase="export" if any(item["type"] == "EXPORT" for item in state["stages"]) else "cdm", partial=False, artifacts=artifact_map(root), pipeline_id=state["pipeline_id"])
    project_history.record(root, completed)
    emitter.emit("pipeline_completed", phase=completed["last_phase"], message="Pipeline completed", details={"pipeline_id": state["pipeline_id"], "output_dir": str(root), "artifacts": artifact_map(root)})
    emitter.emit("job_completed", phase=completed["last_phase"], message="Pipeline completed", details={"pipeline": state, "output_dir": str(root), "artifacts": artifact_map(root)})
    return EXIT_CODES["success"]


def _handle(args):
    root = getattr(args, "output_dir", None)
    if args.command == "preflight":
        emitter = Emitter("preflight") ; result = preflight(args.operation)
        emitter.emit("job_completed" if result["ok"] else "job_failed", phase="preflight", code="ok" if result["ok"] else "preflight_failure", message="Preflight completed", details=result)
        return EXIT_CODES["success"] if result["ok"] else EXIT_CODES["preflight_failure"]
    if args.command == "inspect":
        if not Path(root).expanduser().resolve().is_dir():
            raise MachineError("invalid_input", "output directory does not exist", details={"output_dir": str(root)})
        detail = inspect_output(root); state = detail["job"]; emitter = Emitter(state["job_id"])
        if args.record_activity and detail["project"].get("availability") != "PROJECT_FILES_UNAVAILABLE":
            project_history.record(root, state, activity=True)
        emitter.emit("job_completed", phase=state.get("last_phase"), message="Existing job inspected", details=detail); return 0
    if args.command == "project-history":
        # Explicit persisted output references are the only migration source.
        # Discovery performs inspect-only reconciliation and never starts OCR.
        result = project_history.reconcile(known_outputs=args.known_output, inspect=inspect_output, history_path=args.history_path)
        emitter = Emitter("project-history")
        emitter.emit("job_completed", message="Project history reconciled", details=result); return 0
    if args.command == "project-remove":
        result = project_history.remove(args.project_id, history_path=args.history_path)
        emitter = Emitter("project-history")
        emitter.emit("job_completed", message="Project removed from history", details=result); return 0
    if args.command == "run":
        source = Path(args.sources[0]).expanduser().resolve() if args.sources else None
        # Keep the derived output location on parsed arguments so all error and
        # signal paths use the same durable job identity.
        args.output_dir = source.parent / f"{source.stem}_book" if source else None
        return _run_existing(Emitter("pending"), args.sources, args.core_args)
    if args.command == "pipeline-create":
        root, state, validation = _pipeline_create(args.source, args.definition_json, args.output_dir)
        emitter = Emitter(state["project_id"] or _job_id(root))
        emitter.emit("job_completed", phase="prepare", message="Pipeline configured", details={"output_dir": str(root), "pipeline": state, "validation": validation})
        return EXIT_CODES["success"]
    if args.command == "pipeline-validate":
        try:
            state = sequential_pipeline._load(root)
            if not state:
                raise sequential_pipeline.PipelineError("pipeline does not exist for this project")
            result = sequential_pipeline.validate_definition(state["definition"])
        except sequential_pipeline.PipelineError as error:
            raise MachineError("validation_failure", str(error)) from error
        emitter = Emitter(read_job(root)["job_id"])
        emitter.emit("job_completed", phase="prepare", message="Pipeline validated", details={"pipeline": state, "validation": result})
        return EXIT_CODES["success"]
    if args.command in {"pipeline-start", "pipeline-resume"}:
        return _run_pipeline(root, resume=args.command == "pipeline-resume")
    if args.command == "pipeline-status":
        try:
            state = sequential_pipeline._load(root)
        except sequential_pipeline.PipelineError as error:
            raise MachineError("validation_failure", str(error)) from error
        if not state:
            raise MachineError("invalid_input", "pipeline does not exist for this project")
        emitter = Emitter(read_job(root)["job_id"])
        emitter.emit("job_completed", phase=COMMAND_PHASE[args.command], message="Pipeline status", details={"pipeline": state, "artifacts": artifact_map(root)})
        return EXIT_CODES["success"]
    if args.command == "pipeline-cancel":
        try:
            state = sequential_pipeline._load(root)
            if not state:
                raise sequential_pipeline.PipelineError("pipeline does not exist for this project")
            item = sequential_pipeline.stage(state, state.get("current_stage_id")) if state.get("current_stage_id") else sequential_pipeline.next_pending(state)
            if item is None:
                raise sequential_pipeline.PipelineError("completed pipeline cannot be cancelled")
            sequential_pipeline.mark_paused(root, state, item, code="cancelled", message="Pipeline paused before starting a new stage", cancellation=True)
        except sequential_pipeline.PipelineError as error:
            raise MachineError("validation_failure", str(error)) from error
        update_job(root, status="cancelled", last_phase=_pipeline_phase(item), partial=True, pipeline_id=state["pipeline_id"])
        emitter = Emitter(read_job(root)["job_id"])
        emitter.emit("pipeline_paused", phase=_pipeline_phase(item), code="cancelled", message="Pipeline paused", details={"pipeline_id": state["pipeline_id"], "stage_id": item["stage_id"]})
        return EXIT_CODES["cancelled"]
    state = ensure_job(root); emitter = Emitter(state["job_id"])
    # A user-triggered Core operation is real activity; background history
    # discovery deliberately bypasses this path and leaves ordering unchanged.
    project_history.record(root, state)
    if args.command == "cleanup":
        emitter.emit("cleanup_started", phase="prepare", message="Calculating safe temporary-file cleanup")
        try:
            result = chunk_lifecycle.compact(root)
        except chunk_lifecycle.ChunkPlanError as error:
            raise MachineError("validation_failure", str(error)) from error
        emitter.emit("cleanup_completed" if result.get("cleanup_allowed") else "cleanup_warning", phase="prepare", code="cleanup_complete" if result.get("cleanup_allowed") else "cleanup_not_allowed", message="Temporary-file cleanup completed" if result.get("cleanup_allowed") else "Temporary-file cleanup not yet allowed", details=result)
        emitter.emit("job_completed", phase="prepare", message="Safe cleanup operation completed", details={"result": result, "artifacts": artifact_map(root)})
        return 0
    if args.command == "recover":
        return _invoke(emitter, root, "review", "offline review recovery", lambda: review_recovery.recover(root, surya_results=args.surya_results))
    if args.command == "cdm":
        return _invoke(emitter, root, "cdm", "CDM build", lambda: {"path": str(canonical_document.write_document(canonical_document.build_document(root), root))})
    if args.command == "document-preview":
        return _invoke(emitter, root, "cdm", "CDM preview", lambda: document_preview(root, index=args.index, limit=args.limit))
    if args.command == "export":
        return _invoke(emitter, root, "export", "export", lambda: export_document.export_document(root, args.format))
    if args.command == "translation":
        def translation_progress(event_type, details):
            emitter.emit(event_type, phase="translation", current=details.get("current"), total=details.get("total"),
                         code=event_type, message=event_type.replace("_", " "), details=details)
        return _invoke(emitter, root, "translation", "translation", lambda: translation.translate_document(root,
            source_language=args.source_language, target_language=args.target_language, provider=args.provider, model=args.model,
            glossary_path=args.glossary, endpoint=args.endpoint, timeout=args.timeout, keep_alive=args.keep_alive,
            chunk_size=args.chunk_size, restart=args.restart, block_ids=args.block_ids, progress=translation_progress))
    if args.command == "translation-validate":
        return _invoke(emitter, root, "translation", "translation validation", lambda: translation.validate_translation(root,
            source_language=args.source_language, target_language=args.target_language))
    if args.command == "translation-export":
        return _invoke(emitter, root, "export", "translation export", lambda: export_document.export_translation(root, args.format,
            source_language=args.source_language, target_language=args.target_language))
    if args.command == "final-build":
        return _invoke(emitter, root, "final_build", "final build", lambda: _final_build(root))
    if args.command == "decision":
        return _invoke(emitter, root, "review", "review decision", lambda: review_decisions.record_decision(decisions_path=Path(root)/"review_decisions.json", priority_path=Path(root)/"review_priority.json", book_path=Path(root)/"book.json", item_id=args.item, action=args.action, replacement_text=args.replacement, note=args.note, replace=args.replace))
    if args.command == "baseline-preview":
        return _invoke(emitter, root, "review", "raw Surya baseline preview", lambda: review_decisions.raw_surya_baseline_plan(decisions_path=Path(root)/"review_decisions.json", priority_path=Path(root)/"review_priority.json", book_path=Path(root)/"book.json"))
    if args.command == "baseline-confirm":
        return _invoke(emitter, root, "review", "raw Surya baseline confirmation", lambda: review_decisions.apply_raw_surya_baseline(decisions_path=Path(root)/"review_decisions.json", priority_path=Path(root)/"review_priority.json", book_path=Path(root)/"book.json", confirmed=args.confirmed))
    if args.command == "baseline-revert":
        return _invoke(emitter, root, "review", "raw Surya baseline revert", lambda: review_decisions.revert_raw_surya_baseline(decisions_path=Path(root)/"review_decisions.json", baseline_id=args.baseline_id))
    if args.command == "review":
        workspace = review_workspace.ReviewWorkspace(root)
        filters = {"priority": args.priority, "status": args.status, "page": args.page}
        ids = workspace.filtered_ids(filters)
        emitter.emit("job_completed", phase="review", message="Review queue queried", details={
            "filters": {key: value for key, value in filters.items() if value is not None},
            "count": len(ids), "item_ids": ids, "progress": workspace.progress(),
        }); return 0
    if args.command == "review-item":
        workspace = review_workspace.ReviewWorkspace(root)
        filters = {"priority": args.priority, "status": args.status, "page": args.page}
        ids = workspace.filtered_ids(filters)
        if args.item:
            if args.item not in ids: raise MachineError("invalid_input", "review item is not in the requested queue", details={"item_id": args.item})
            index = ids.index(args.item)
        else:
            index = min(max(args.index, 0), max(0, len(ids) - 1))
        payload = {"filters": {key: value for key, value in filters.items() if value is not None}, "count": len(ids), "index": index, "progress": workspace.progress()}
        if ids: payload["item"] = workspace.item_payload(ids[index])
        emitter.emit("job_completed", phase="review", message="Review item queried", details=payload); return 0
    if args.command == "review-clusters":
        workspace = review_workspace.ReviewWorkspace(root)
        filters = {"priority": args.priority, "status": args.status, "category": args.category}
        clusters = workspace.filtered_clusters(filters)
        if args.cluster:
            if args.cluster not in {value["cluster_id"] for value in clusters}:
                raise MachineError("invalid_input", "cluster is not in the requested queue", details={"cluster_id": args.cluster})
            cluster = workspace.cluster_payload(args.cluster)
            index = [value["cluster_id"] for value in clusters].index(args.cluster)
        else:
            index = min(max(args.index, 0), max(0, len(clusters) - 1))
            cluster = workspace.cluster_payload(clusters[index]["cluster_id"]) if clusters else None
        emitter.emit("job_completed", phase="review", message="Review clusters queried", details={
            "filters": {key: value for key, value in filters.items() if value is not None}, "count": len(clusters), "index": index,
            "clusters": [{key: cluster.get(key) for key in ("cluster_id", "category", "member_count", "batch_eligible", "priority_distribution")} for cluster in clusters],
            "cluster": cluster, "progress": workspace.progress(),
        }); return 0
    if args.command in {"batch-preview", "batch-confirm", "batch-revert"}:
        workspace = review_workspace.ReviewWorkspace(root)
        if args.command == "batch-preview": result = workspace.batch_preview(args.cluster, args.action, args.exclude)
        elif args.command == "batch-confirm": result = workspace.batch_confirm(args.cluster, args.action, args.exclude, args.confirmed)
        else: result = workspace.batch_revert(args.batch_id)
        emitter.emit("job_completed", phase="review", message=f"{args.command} completed", details={"result": result, "progress": workspace.progress()}); return 0
    raise MachineError("invalid_input", "unsupported machine operation")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol-version", action="version", version=PROTOCOL_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("preflight"); p.add_argument("--operation", choices=("status", "run", "native-text", "export", "cdm", "final-build", "review", "translation"), default="status")
    p = sub.add_parser("inspect"); p.add_argument("output_dir", type=Path); p.add_argument("--record-activity", action="store_true")
    p = sub.add_parser("project-history"); p.add_argument("--known-output", action="append", default=[]); p.add_argument("--history-path")
    p = sub.add_parser("project-remove"); p.add_argument("project_id"); p.add_argument("--history-path")
    p = sub.add_parser("cleanup"); p.add_argument("output_dir", type=Path)
    p = sub.add_parser("run"); p.add_argument("sources", nargs="+"); p.add_argument("core_args", nargs=argparse.REMAINDER)
    p = sub.add_parser("pipeline-create"); p.add_argument("--definition-json", required=True); p.add_argument("--output-dir", type=Path); p.add_argument("source")
    for command in ("pipeline-validate", "pipeline-start", "pipeline-status", "pipeline-resume", "pipeline-cancel"):
        p = sub.add_parser(command); p.add_argument("output_dir", type=Path)
    p = sub.add_parser("cdm"); p.add_argument("output_dir", type=Path)
    p = sub.add_parser("document-preview"); p.add_argument("output_dir", type=Path); p.add_argument("--index", type=int, default=0); p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("recover"); p.add_argument("output_dir", type=Path); p.add_argument("--surya-results")
    p = sub.add_parser("export"); p.add_argument("output_dir", type=Path); p.add_argument("--format", action="append", required=True, choices=[*export_document.FORMATS, "all"])
    def translation_options(p, *, provider=True):
        p.add_argument("output_dir", type=Path); p.add_argument("--source-language", required=True, choices=("en", "ru")); p.add_argument("--target-language", required=True, choices=("en", "ru"))
        if provider:
            p.add_argument("--provider", default="ollama", choices=("ollama",)); p.add_argument("--model", required=True); p.add_argument("--glossary"); p.add_argument("--endpoint", default="http://127.0.0.1:11434"); p.add_argument("--timeout", type=int, default=120); p.add_argument("--keep-alive", default=translation.DEFAULT_MODEL_KEEP_ALIVE); p.add_argument("--chunk-size", type=int, default=translation.DEFAULT_LOGICAL_UNIT_CHUNK_SIZE); p.add_argument("--block-id", action="append", dest="block_ids", help="Translate only a source-ordered logical-unit pilot; repeat for each owned unit"); p.add_argument("--restart", action="store_true")
    translation_options(sub.add_parser("translation"))
    translation_options(sub.add_parser("translation-validate"), provider=False)
    p = sub.add_parser("translation-export"); p.add_argument("output_dir", type=Path); p.add_argument("--source-language", required=True, choices=("en", "ru")); p.add_argument("--target-language", required=True, choices=("en", "ru")); p.add_argument("--format", action="append", required=True, choices=[*export_document.FORMATS, "all"])
    p = sub.add_parser("final-build"); p.add_argument("output_dir", type=Path)
    p = sub.add_parser("decision"); p.add_argument("output_dir", type=Path); p.add_argument("--item", required=True); p.add_argument("--action", choices=sorted(review_decisions.TEXT_ACTIONS), required=True); p.add_argument("--replacement"); p.add_argument("--note"); p.add_argument("--replace", action="store_true")
    p = sub.add_parser("baseline-preview"); p.add_argument("output_dir", type=Path)
    p = sub.add_parser("baseline-confirm"); p.add_argument("output_dir", type=Path); p.add_argument("--confirmed", action="store_true")
    p = sub.add_parser("baseline-revert"); p.add_argument("output_dir", type=Path); p.add_argument("--baseline-id", required=True)
    def review_filters(p, *, page=True):
        p.add_argument("--priority", choices=("HIGH", "MEDIUM", "LOW", "INFO")); p.add_argument("--status", choices=("unresolved", "resolved_changed", "resolved_no_change", "deferred", "stale", "conflict", "invalid"))
        if page: p.add_argument("--page", type=int)
    p = sub.add_parser("review"); p.add_argument("output_dir", type=Path); review_filters(p)
    p = sub.add_parser("review-item"); p.add_argument("output_dir", type=Path); review_filters(p); p.add_argument("--item"); p.add_argument("--index", type=int, default=0)
    p = sub.add_parser("review-clusters"); p.add_argument("output_dir", type=Path); review_filters(p, page=False); p.add_argument("--category"); p.add_argument("--cluster"); p.add_argument("--index", type=int, default=0)
    for command in ("batch-preview", "batch-confirm"):
        p = sub.add_parser(command); p.add_argument("output_dir", type=Path); p.add_argument("--cluster", required=True); p.add_argument("--action", required=True); p.add_argument("--exclude", action="append", default=[])
        if command == "batch-confirm": p.add_argument("--confirmed", action="store_true")
    p = sub.add_parser("batch-revert"); p.add_argument("output_dir", type=Path); p.add_argument("--batch-id", required=True)
    args = parser.parse_args(argv)
    def _cancel_signal(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _cancel_signal)
    signal.signal(signal.SIGTERM, _cancel_signal)
    try:
        return _handle(args)
    except KeyboardInterrupt:
        # A signal during an operation is reported as cancellation, not prose.
        root = getattr(args, "output_dir", None)
        if root:
            try:
                pipeline = sequential_pipeline._load(root)
                if pipeline and pipeline.get("current_stage_id"):
                    item = sequential_pipeline.stage(pipeline, pipeline["current_stage_id"])
                    sequential_pipeline.mark_paused(root, pipeline, item, code="cancelled", message="Pipeline cancelled at a safe boundary", cancellation=True)
            except sequential_pipeline.PipelineError:
                pass
            state = update_job(root, status="cancelled", partial=True)
            Emitter(state["job_id"]).emit("job_cancelled", phase=state.get("last_phase"), code="cancelled", message="Job cancelled")
        return EXIT_CODES["cancelled"]
    except MachineError as error:
        root = getattr(args, "output_dir", None)
        existing_root = root and Path(root).expanduser().resolve().is_dir()
        job_id = ensure_job(root)["job_id"] if existing_root else "machine"
        if existing_root: update_job(root, status="failed", error_code=error.code, error_details=error.details)
        Emitter(job_id).emit("job_failed", phase=COMMAND_PHASE.get(getattr(args, "command", None)), code=error.code, message=str(error), details=error.details)
        return EXIT_CODES.get(error.code, EXIT_CODES["validation_failure"])
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        root = getattr(args, "output_dir", None); job_id = ensure_job(root)["job_id"] if root else "machine"
        if root: update_job(root, status="failed", error_code="validation_failure")
        Emitter(job_id).emit("job_failed", code="validation_failure", message=str(error), details={"type": type(error).__name__})
        return EXIT_CODES["validation_failure"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
