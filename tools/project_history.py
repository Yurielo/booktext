"""Small, Core-owned index of resumable local BookText projects.

The index is deliberately a *discovery aid*.  It stores no processing state:
every row is reconciled with the output directory through the machine protocol
before it is shown to the native app.  Removing a row only removes that index
record; it never removes a source document or any BookText artifact.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tools import review_decisions


SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "booktext.project_history"
DEFAULT_FILENAME = "project-history.json"
_TRANSIENT_PREFIXES = ("/tmp/", "/private/tmp/", "/private/var/folders/")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_path():
    configured = os.environ.get("BOOKTEXT_HISTORY_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "BookText" / DEFAULT_FILENAME


def _load(path):
    path = Path(path)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "artifact_type": ARTIFACT_TYPE, "entries": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        # A corrupt user preference must not make an existing BookText output
        # unavailable.  The caller receives a controlled warning and can still
        # inspect a project directly.
        return {"schema_version": SCHEMA_VERSION, "artifact_type": ARTIFACT_TYPE,
                "entries": [], "warning": f"history index unreadable: {error}"}
    if not isinstance(value, dict) or value.get("artifact_type") != ARTIFACT_TYPE:
        return {"schema_version": SCHEMA_VERSION, "artifact_type": ARTIFACT_TYPE,
                "entries": [], "warning": "history index has an unsupported schema"}
    entries = value.get("entries")
    if not isinstance(entries, list):
        value["entries"] = []
        value["warning"] = "history index entries are invalid"
    value.setdefault("schema_version", SCHEMA_VERSION)
    return value


def _write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    review_decisions.atomic_write_json(path, value)


def _name(root, state):
    identity = root / "document_identity.json"
    if identity.is_file():
        try:
            value = json.loads(identity.read_text(encoding="utf-8"))
            for key in ("display_name", "title", "source_name", "filename"):
                if isinstance(value.get(key), str) and value[key].strip():
                    return value[key].strip()
        except (OSError, json.JSONDecodeError):
            pass
    source = state.get("source") if isinstance(state, dict) else []
    if isinstance(source, list) and source and isinstance(source[0], str):
        return Path(source[0]).stem
    name = root.name
    return name[:-5] if name.endswith("_book") else name


def _is_transient_workspace(root):
    """Do not let development/test temp directories become user recents.

    Managed BookText outputs are user-selected persistent locations.  A caller
    can still explicitly use a temporary index for an integration test, but the
    default user index never acquires fragile operating-system temp paths.
    """
    return str(Path(root).expanduser()).startswith(_TRANSIENT_PREFIXES)


def record(root, state, *, history_path=None, activity=True):
    """Upsert a stable Core job ID without ever duplicating a reopen."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir() or not isinstance(state, dict) or not isinstance(state.get("job_id"), str):
        return None
    if history_path is None and _is_transient_workspace(root):
        return None
    path = Path(history_path).expanduser().resolve() if history_path else default_path()
    payload = _load(path); entries = [entry for entry in payload["entries"] if isinstance(entry, dict)]
    project_id = state["job_id"]
    existing = next((entry for entry in entries if entry.get("project_id") == project_id), None)
    now = _now()
    value = {
        "project_id": project_id,
        "output_dir": str(root),
        "source": [item for item in state.get("source", []) if isinstance(item, str)],
        "name": _name(root, state),
        "created_at": existing.get("created_at", now) if existing else now,
        "last_activity_at": now if activity or not existing else existing.get("last_activity_at", now),
    }
    if existing:
        entries[entries.index(existing)] = value
    else:
        entries.append(value)
    payload["schema_version"] = SCHEMA_VERSION; payload["artifact_type"] = ARTIFACT_TYPE; payload["entries"] = entries
    payload.pop("warning", None); _write(path, payload)
    return value


def remove(project_id, *, history_path=None):
    path = Path(history_path).expanduser().resolve() if history_path else default_path()
    payload = _load(path); before = len(payload["entries"])
    payload["entries"] = [entry for entry in payload["entries"] if not (isinstance(entry, dict) and entry.get("project_id") == project_id)]
    _write(path, payload)
    return {"removed": before != len(payload["entries"]), "project_id": project_id, "project_data_deleted": False}


def reconcile(*, known_outputs, inspect, history_path=None):
    """Register known managed roots, then project current Core truth onto rows.

    `inspect` is injected by the machine protocol to avoid a second state
    implementation here.  Discovery intentionally does no OCR/model work.
    """
    path = Path(history_path).expanduser().resolve() if history_path else default_path()
    payload = _load(path)
    warnings = [payload["warning"]] if payload.get("warning") else []
    # Migration is conservative: only explicit persisted references are added.
    for raw in known_outputs or []:
        root = Path(raw).expanduser()
        if not root.is_dir():
            warnings.append(f"known project unavailable: {root}")
            continue
        try:
            detail = inspect(root)
            record(root, detail.get("job", {}), history_path=path, activity=False)
        except Exception:
            warnings.append(f"known project could not be inspected: {root}")
    payload = _load(path)
    rows = []
    transient_entries_ignored = False
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("project_id"), str) or not isinstance(entry.get("output_dir"), str):
            warnings.append("malformed history entry ignored")
            continue
        root = Path(entry["output_dir"])
        if history_path is None and _is_transient_workspace(root):
            transient_entries_ignored = True
            continue
        if not root.is_dir():
            rows.append({**entry, "availability": "PROJECT_FILES_UNAVAILABLE", "state": "FAILED",
                         "action": "Locate project files", "detail": "Project files unavailable"})
            continue
        try:
            detail = inspect(root)
            project = detail.get("project", {})
            rows.append({**entry, **project, "availability": project.get("availability", "AVAILABLE")})
        except Exception:
            rows.append({**entry, "availability": "PROJECT_FILES_UNAVAILABLE", "state": "FAILED",
                         "action": "Locate project files", "detail": "Project files unavailable"})
    rows.sort(key=lambda item: item.get("last_activity_at", ""), reverse=True)
    if transient_entries_ignored:
        warnings.append("temporary development entries are ignored by Recent Projects")
    return {"schema_version": SCHEMA_VERSION, "artifact_type": ARTIFACT_TYPE, "projects": rows, "warnings": warnings}
