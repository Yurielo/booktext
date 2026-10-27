"""Prepare and publish BookText's existing native activation evidence centrally.

This adapter never deploys or activates BookText. It records the installed GUI
and configured external Core after the existing native lifecycle has succeeded,
then delegates registry writes to the Four-Product Architecture publisher.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


APP_ID = "local.yurielo.BookText"
DEFAULT_APP = Path("/Applications/BookText.app")
DEFAULT_SUPPORT = Path.home() / "Library/Application Support/BookText/Activation"
REGISTRY_URL = "https://github.com/Yurielo/four-product-architecture.git"


class ActivationError(RuntimeError):
    pass


def _run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ActivationError(f"command failed ({result.returncode}): {args[0]}: {detail}")
    return result.stdout.strip()


def file_list_digest(root: Path) -> str:
    """Match Four-Product Architecture's sha256-file-list-v1 algorithm."""
    root = root.resolve(strict=True)
    records: list[bytes] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = "./" + path.relative_to(root).as_posix()
        records.append((hashlib.sha256(path.read_bytes()).hexdigest() + "  " + relative + "\n").encode())
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item.split(b"  ", 1)[1]):
        digest.update(record)
    return digest.hexdigest()


def _source_commit(value: object) -> str | None:
    if isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def inspect_core(script_path: Path) -> dict:
    script = script_path.expanduser().resolve(strict=True)
    if script.name != "booktext.py" or not script.is_file():
        raise ActivationError("configured BookText Core must point to its booktext.py entrypoint")
    root = Path(_run("git", "-C", str(script.parent), "rev-parse", "--show-toplevel")).resolve()
    if root != script.parent:
        raise ActivationError("configured BookText Core entrypoint must be at the root of its Git checkout")
    status = _run("git", "-C", str(root), "status", "--porcelain", "--untracked-files=all")
    if status:
        raise ActivationError("configured BookText Core source tree is not clean; refusing an ambiguous source identity")
    commit = _run("git", "-C", str(root), "rev-parse", "HEAD")
    tree = _run("git", "-C", str(root), "rev-parse", "HEAD^{tree}")
    return {"script": str(script), "root": str(root), "source_commit": commit, "tree": tree}


def inspect_app(app_path: Path) -> dict:
    app = app_path.expanduser().resolve(strict=True)
    if app.suffix != ".app" or not app.is_dir():
        raise ActivationError("BookText application path must be an installed .app bundle")
    info_path = app / "Contents/Info.plist"
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    if info.get("CFBundleIdentifier") != APP_ID:
        raise ActivationError("installed application is not the native BookText GUI")
    executable = app / "Contents/MacOS" / str(info.get("CFBundleExecutable", ""))
    if not executable.is_file():
        raise ActivationError("installed BookText executable is missing")
    _run("codesign", "--verify", "--deep", "--strict", str(app))
    return {
        "path": str(app),
        "bundle_id": APP_ID,
        "version": str(info.get("CFBundleShortVersionString", "")),
        "build": str(info.get("CFBundleVersion", "")),
        "source_commit": _source_commit(info.get("BookTextSourceCommit")),
        "digest": file_list_digest(app),
    }


def configured_core_script() -> Path:
    value = _run("defaults", "read", APP_ID, "booktext.core.path")
    if not value:
        raise ActivationError("BookText has no configured external Core path")
    return Path(value)


def load_registry_module(registry_root: Path):
    root = registry_root.expanduser().resolve(strict=True)
    module_path = root / "registry.py"
    if not module_path.is_file() or (root / ".git").exists() is False:
        raise ActivationError("provide a Four-Product Architecture Git checkout")
    if _run("git", "-C", str(root), "remote", "get-url", "origin").removesuffix(".git") != REGISTRY_URL.removesuffix(".git"):
        raise ActivationError("registry checkout origin is not Yurielo/four-product-architecture")
    spec = importlib.util.spec_from_file_location("booktext_central_registry", module_path)
    if spec is None or spec.loader is None:
        raise ActivationError("could not load the central registry publisher")
    registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry)
    return root, registry


def read_remote_state(root: Path, registry) -> tuple[str, dict, dict]:
    head = registry.remote_head(root)
    schema = registry.decode(registry.at(root, head, registry.SCHEMA))
    state = registry.decode(registry.at(root, head, "registry.json"))
    registry.validate(state, schema, lambda path: registry.at(root, head, path))
    return head, schema, state


def _receipt_time(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActivationError("native activation time must be an ISO-8601 string or null")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ActivationError("native activation time is not valid ISO-8601") from error
    return value


def build_event(*, registry, state: dict, app: dict, core: dict, target: str,
                native_reference: str, native_evidence: dict, activated_at: str | None,
                recorded_at: str, action: str = "activate",
                rollback_of: str | None = None) -> tuple[dict, bytes]:
    current = state["products"]["booktext"]["current"]
    previous = next((event for event in state["history"] if event["event_id"] == current), None)
    if current is None or previous is None:
        raise ActivationError("BookText must already have a central activation; initial registration is out of scope")
    gui_commit = app["source_commit"]
    if not app["version"] or not target or not native_reference.strip():
        raise ActivationError("installed version, actual target, and native activation reference are required")
    release = {
        "native_ref": native_reference,
        "version": app["version"],
        "build": app["build"] or None,
        "source_commit": gui_commit,
        "artifacts": [
            {"role": "installed-gui", "location": app["path"], "source_commit": gui_commit,
             "digest": {"algorithm": "sha256-file-list-v1", "value": app["digest"]}},
            {"role": "installed-core-tracked-tree", "location": core["root"], "source_commit": core["source_commit"],
             "digest": {"algorithm": "git-sha1-tree", "value": core["tree"]}},
        ],
    }
    dependencies = []
    if action == "rollback":
        target_event = next((item for item in state["history"] if item["event_id"] == rollback_of), None)
        if target_event is None or target_event["product"] != "booktext":
            raise ActivationError("rollback target must be an earlier central BookText activation")
        if rollback_of == current:
            raise ActivationError("rollback target is already the current BookText event")
        measured_release = {**release, "native_ref": target_event["release"]["native_ref"]}
        if measured_release != target_event["release"] or target != target_event["target"] or dependencies != target_event["dependencies"]:
            raise ActivationError("installed rollback identities do not exactly match the historical activation")
        # The registry's rollback semantics preserve historical release bytes,
        # including their original native release reference. The new native
        # rollback proof is bound separately to the new event subject.
        release = target_event["release"]
        target = target_event["target"]
        dependencies = target_event["dependencies"]
    elif action != "activate" or rollback_of is not None:
        raise ActivationError("activation action must be 'activate' or a valid rollback")
    event = {
        "event_id": "",
        "product": "booktext",
        "generation": previous["generation"] + 1,
        "predecessor": current,
        "action": action,
        "rollback_of": rollback_of,
        "target": target,
        "activated_at": activated_at,
        "recorded_at": recorded_at,
        "release": release,
        "dependencies": dependencies,
        "evidence": [],
    }
    receipt = {
        "kind": "native_activation",
        "status": "SUCCEEDED",
        "subject_sha256": registry.subject_hash(event),
        "native_reference": native_reference,
        "native_evidence": {
            "activation": native_evidence,
            "observation": {"gui": app, "configured_core": core, "target": target},
        },
    }
    payload = registry.encode(receipt)
    ref_name = hashlib.sha256(native_reference.encode("utf-8")).hexdigest()[:32]
    event["evidence"] = [{"path": f"evidence/booktext-activation-{ref_name}.json", "sha256": registry.sha256(payload)}]
    event["event_id"] = registry.event_id(event)
    return event, payload


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def prepare(args) -> dict:
    support = args.support.expanduser().resolve()
    identity = hashlib.sha256(args.native_reference.encode("utf-8")).hexdigest()
    destination = support / identity
    if destination.exists():
        event_path = destination / "event.json"
        if not event_path.is_file():
            raise ActivationError(f"incomplete activation receipt already exists at {destination}; preserve it for review")
        event = json.loads(event_path.read_text(encoding="utf-8"))
        return {"status": "ALREADY_PREPARED", "event_id": event.get("event_id"), "receipt": str(destination)}

    with args.native_evidence.expanduser().open("r", encoding="utf-8") as handle:
        native = json.load(handle)
    if not isinstance(native, dict) or native.get("status") != "SUCCEEDED":
        raise ActivationError("native evidence must be a JSON object with status=SUCCEEDED")
    if not isinstance(native.get("native_evidence"), dict) or not native["native_evidence"]:
        raise ActivationError("native evidence must retain the existing successful Deployment/Activation evidence")
    reference = native.get("native_reference") or args.native_reference
    if reference != args.native_reference:
        raise ActivationError("native evidence reference differs from --native-reference")
    activated_at = _receipt_time(native.get("activated_at"))
    _, registry = load_registry_module(args.registry_root)
    _, _, state = read_remote_state(args.registry_root, registry)
    app = inspect_app(args.app)
    core_script = args.core_script if args.core_script else configured_core_script()
    core = inspect_core(core_script)
    event, payload = build_event(
        registry=registry, state=state, app=app, core=core, target=args.target,
        native_reference=args.native_reference, native_evidence=native,
        activated_at=activated_at, recorded_at=datetime.now(timezone.utc).isoformat(),
        action=args.action, rollback_of=args.rollback_of,
    )
    support.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".activation-", dir=support))
    try:
        _write_atomic(staging / "event.json", registry.encode(event))
        _write_atomic(staging / "native-activation.json", payload)
        try:
            os.rename(staging, destination)
        except FileExistsError:
            shutil.rmtree(staging)
            return {"status": "ALREADY_PREPARED", "event_id": None, "receipt": str(destination)}
    except BaseException:
        if staging.exists() and staging.parent == support:
            shutil.rmtree(staging)
        raise
    return {"status": "ACTIVATED / PUBLICATION_PENDING", "event_id": event["event_id"], "receipt": str(destination)}


def publish(args) -> dict:
    receipt_dir = args.receipt.expanduser().resolve(strict=True)
    event_path = receipt_dir / "event.json"
    proof_path = receipt_dir / "native-activation.json"
    if not event_path.is_file() or not proof_path.is_file():
        raise ActivationError("activation receipt directory must contain event.json and native-activation.json")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    proof = proof_path.read_bytes()
    registry_root, registry = load_registry_module(args.registry_root)
    if not isinstance(event, dict) or event.get("product") != "booktext":
        raise ActivationError("pending event is not a BookText activation")
    refs = event.get("evidence", [])
    if event.get("event_id") != registry.event_id(event):
        raise ActivationError("pending event content does not match its event ID")
    if len(refs) != 1 or not isinstance(refs[0], dict):
        raise ActivationError("pending event must reference exactly one retained native receipt")
    evidence_path = refs[0].get("path")
    if not isinstance(evidence_path, str) or not re.fullmatch(r"evidence/booktext-activation-[a-z0-9-]+\.json", evidence_path):
        raise ActivationError("pending event has an unsafe or unexpected evidence path")
    if refs[0].get("sha256") != registry.sha256(proof):
        raise ActivationError("pending native evidence does not match the immutable event")
    native_receipt = registry.decode(proof)
    if (not isinstance(native_receipt, dict) or native_receipt.get("kind") != "native_activation"
            or native_receipt.get("status") != "SUCCEEDED"
            or native_receipt.get("subject_sha256") != registry.subject_hash(event)):
        raise ActivationError("pending native receipt does not attest this successful event")
    try:
        _, _, state = read_remote_state(registry_root, registry)
    except Exception as error:
        return {"status": "PUBLICATION_PENDING", "event_id": event.get("event_id"),
                "receipt": str(receipt_dir), "message": str(error)}
    if any(item == event for item in state["history"]):
        return {"status": "ALREADY_PUBLISHED", "event_id": event["event_id"],
                "current": state["products"]["booktext"]["current"]}
    if state["products"]["booktext"]["current"] != event.get("predecessor"):
        return {"status": "PUBLICATION_STALE", "event_id": event.get("event_id"),
                "current": state["products"]["booktext"]["current"],
                "message": "BookText has a later central activation; preserve this receipt and do not reseal it."}

    # The central publisher needs the evidence bytes in its checkout. Use a
    # disposable clone so a retry never dirties the user's central worktree.
    try:
        with tempfile.TemporaryDirectory(prefix="booktext-central-publish-") as temporary:
            clone = Path(temporary) / "four-product-architecture"
            _run("git", "clone", "--quiet", REGISTRY_URL, str(clone))
            proof_target = clone / evidence_path
            if not proof_target.resolve().is_relative_to(clone.resolve()):
                raise ActivationError("pending native evidence path escapes the central repository")
            if proof_target.exists():
                if proof_target.read_bytes() != proof:
                    raise ActivationError("central evidence path is already occupied by different bytes")
            else:
                _write_atomic(proof_target, proof)
            head = registry.remote_head(clone)
            result = registry.publish(clone, event, head)
            return {**result, "receipt": str(receipt_dir)}
    except Exception as error:
        try:
            _, _, latest = read_remote_state(registry_root, registry)
            current = latest["products"]["booktext"]["current"]
            if any(item == event for item in latest["history"]):
                return {"status": "ALREADY_PUBLISHED", "event_id": event["event_id"],
                        "current": current, "receipt": str(receipt_dir)}
            if current != event.get("predecessor"):
                return {"status": "PUBLICATION_STALE", "event_id": event.get("event_id"),
                        "current": current, "receipt": str(receipt_dir),
                        "message": "BookText has a later central activation; preserve this receipt and do not reseal it."}
        except Exception:
            pass
        return {"status": "PUBLICATION_PENDING", "event_id": event.get("event_id"),
                "receipt": str(receipt_dir), "message": str(error)}


def complete(args) -> dict:
    """Persist the post-Activation receipt, then publish it in the same call."""
    prepared = prepare(args)
    return publish(argparse.Namespace(registry_root=args.registry_root,
                                      receipt=Path(prepared["receipt"])))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("prepare", help="record exact installed identities after native Activation")
    make.add_argument("--registry-root", type=Path, required=True)
    make.add_argument("--native-reference", required=True)
    make.add_argument("--native-evidence", type=Path, required=True,
                      help="existing successful native Deployment/Activation evidence JSON")
    make.add_argument("--target", required=True, help="actual native deployment target label")
    make.add_argument("--action", choices=("activate", "rollback"), default="activate")
    make.add_argument("--rollback-of", help="prior BookText event ID for a completed native rollback")
    make.add_argument("--app", type=Path, default=DEFAULT_APP)
    make.add_argument("--core-script", type=Path,
                      help="optional explicit Core entrypoint; otherwise read BookText's configured path")
    make.add_argument("--support", type=Path, default=DEFAULT_SUPPORT)
    finish = commands.add_parser("complete", help="record a successful native Activation and publish it")
    finish.add_argument("--registry-root", type=Path, required=True)
    finish.add_argument("--native-reference", required=True)
    finish.add_argument("--native-evidence", type=Path, required=True,
                        help="existing successful native Deployment/Activation evidence JSON")
    finish.add_argument("--target", required=True, help="actual native deployment target label")
    finish.add_argument("--action", choices=("activate", "rollback"), default="activate")
    finish.add_argument("--rollback-of", help="prior BookText event ID for a completed native rollback")
    finish.add_argument("--app", type=Path, default=DEFAULT_APP)
    finish.add_argument("--core-script", type=Path,
                        help="optional explicit Core entrypoint; otherwise read BookText's configured path")
    finish.add_argument("--support", type=Path, default=DEFAULT_SUPPORT)
    retry = commands.add_parser("publish", help="publish or safely retry an immutable pending receipt")
    retry.add_argument("--registry-root", type=Path, required=True)
    retry.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = (prepare(args) if args.command == "prepare" else
                  complete(args) if args.command == "complete" else publish(args))
    except (ActivationError, OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "ACTIVATION_RECEIPT_REJECTED", "message": str(error)}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"PUBLISHED", "ALREADY_PUBLISHED", "ALREADY_PREPARED", "ACTIVATED / PUBLICATION_PENDING"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
