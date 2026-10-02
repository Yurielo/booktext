"""Historical diagnostic tooling requires an explicit durable evidence destination."""
import os
import tempfile
from pathlib import Path


def diagnostic_path(name):
    configured = os.environ.get("BOOKTEXT_DIAGNOSTIC_DIR")
    if not configured or not Path(configured).is_absolute():
        raise RuntimeError("Set BOOKTEXT_DIAGNOSTIC_DIR to an explicit absolute persistent evidence directory")
    root = Path(configured).resolve()
    forbidden = [Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()]
    if any(root == p or p in root.parents for p in forbidden):
        raise RuntimeError("Diagnostic evidence must not be retained in system TMP")
    return root / name
