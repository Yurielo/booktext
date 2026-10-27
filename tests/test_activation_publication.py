import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import activation_publication as activation


class FakeRegistry:
    def subject_hash(self, value):
        return "subject-hash"

    def encode(self, value):
        return (json.dumps(value, sort_keys=True) + "\n").encode()

    def decode(self, value):
        return json.loads(value)

    def sha256(self, value):
        return hashlib.sha256(value).hexdigest()

    def event_id(self, value):
        return "sha256:event"


class ActivationPublicationTests(unittest.TestCase):
    def _pending_receipt(self, root: Path, *, predecessor="sha256:prior"):
        state = {"products": {"booktext": {"current": predecessor}}, "history": [{"event_id": predecessor, "generation": 1}]}
        app = {"path": "/Applications/BookText.app", "version": "0.1.1", "build": "2",
               "source_commit": None, "digest": "b" * 64}
        core = {"root": "/Core", "source_commit": "a" * 40, "tree": "c" * 40}
        event, proof = activation.build_event(
            registry=FakeRegistry(), state=state, app=app, core=core,
            target="local-primary", native_reference="https://example.test/activation",
            native_evidence={"status": "SUCCEEDED", "native_evidence": {"activation": "completed"}},
            activated_at=None, recorded_at="2026-10-27T00:00:00+00:00",
        )
        receipt = root / "receipt"
        receipt.mkdir()
        (receipt / "event.json").write_text(json.dumps(event), encoding="utf-8")
        (receipt / "native-activation.json").write_bytes(proof)
        return event, proof, receipt

    def test_file_list_digest_matches_central_algorithm_and_ignores_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Contents").mkdir()
            (root / "Contents/Info.plist").write_bytes(b"plist")
            (root / "Contents/MacOS").mkdir()
            (root / "Contents/MacOS/BookText").write_bytes(b"executable")
            (root / "Contents/alias").symlink_to(root / "Contents/Info.plist")
            records = []
            for relative in ("./Contents/Info.plist", "./Contents/MacOS/BookText"):
                data = (root / relative[2:]).read_bytes()
                records.append(f"{hashlib.sha256(data).hexdigest()}  {relative}\n".encode())
            self.assertEqual(hashlib.sha256(b"".join(sorted(records, key=lambda row: row.split(b"  ", 1)[1]))).hexdigest(), activation.file_list_digest(root))

    def test_source_commit_is_only_attested_when_it_is_exactly_git_sha1(self):
        self.assertEqual("a" * 40, activation._source_commit("a" * 40))
        self.assertIsNone(activation._source_commit("unavailable"))
        self.assertIsNone(activation._source_commit("a" * 39))
        self.assertIsNone(activation._source_commit("g" * 40))

    def test_composite_activation_keeps_gui_and_core_identities_separate(self):
        prior = {
            "event_id": "sha256:prior",
            "generation": 1,
        }
        state = {"products": {"booktext": {"current": "sha256:prior"}}, "history": [prior]}
        app = {"path": "/Applications/BookText.app", "version": "0.1.1", "build": "2",
               "source_commit": None, "digest": "b" * 64}
        core = {"root": "/Library/Application Support/BookText/Core",
                "source_commit": "a" * 40, "tree": "c" * 40}
        event, payload = activation.build_event(
            registry=FakeRegistry(), state=state, app=app, core=core,
            target="/Applications/BookText.app", native_reference="https://example.test/activation",
            native_evidence={"status": "SUCCEEDED", "native_evidence": {"activation": "completed"}}, activated_at=None,
            recorded_at="2026-10-27T00:00:00+00:00",
        )
        self.assertIsNone(event["release"]["source_commit"])
        self.assertIsNone(event["release"]["artifacts"][0]["source_commit"])
        self.assertEqual("a" * 40, event["release"]["artifacts"][1]["source_commit"])
        self.assertEqual("sha256-file-list-v1", event["release"]["artifacts"][0]["digest"]["algorithm"])
        self.assertEqual("git-sha1-tree", event["release"]["artifacts"][1]["digest"]["algorithm"])
        self.assertEqual("sha256:event", event["event_id"])
        self.assertIn(b'"kind": "native_activation"', payload)

    def test_rollback_reuses_exact_historical_release_and_adds_new_evidence(self):
        old_release = {
            "native_ref": "https://example.test/original",
            "version": "0.1.0",
            "build": "1",
            "source_commit": None,
            "artifacts": [
                {"role": "installed-gui", "location": "/Applications/BookText.app", "source_commit": None,
                 "digest": {"algorithm": "sha256-file-list-v1", "value": "b" * 64}},
                {"role": "installed-core-tracked-tree", "location": "/Core", "source_commit": "a" * 40,
                 "digest": {"algorithm": "git-sha1-tree", "value": "c" * 40}},
            ],
        }
        old = {"event_id": "sha256:old", "product": "booktext", "generation": 1,
               "release": old_release, "dependencies": [], "target": "/Applications/BookText.app"}
        current = {"event_id": "sha256:current", "generation": 2}
        state = {"products": {"booktext": {"current": "sha256:current"}}, "history": [old, current]}
        app = {"path": "/Applications/BookText.app", "version": "0.1.0", "build": "1",
               "source_commit": None, "digest": "b" * 64}
        core = {"root": "/Core", "source_commit": "a" * 40, "tree": "c" * 40}
        event, _ = activation.build_event(
            registry=FakeRegistry(), state=state, app=app, core=core,
            target="/Applications/BookText.app", native_reference="https://example.test/rollback",
            native_evidence={"status": "SUCCEEDED", "native_evidence": {"rollback": "verified"}},
            activated_at=None, recorded_at="2026-10-27T00:00:00+00:00",
            action="rollback", rollback_of="sha256:old",
        )
        self.assertEqual("rollback", event["action"])
        self.assertEqual("sha256:old", event["rollback_of"])
        self.assertEqual(old_release, event["release"])
        self.assertNotEqual(old["event_id"], event["event_id"])

    def test_rollback_cannot_claim_different_installed_bytes(self):
        old = {"event_id": "sha256:old", "product": "booktext", "generation": 1,
               "release": {"native_ref": "old"}, "dependencies": [], "target": "target"}
        current = {"event_id": "sha256:current", "generation": 2}
        state = {"products": {"booktext": {"current": "sha256:current"}}, "history": [old, current]}
        app = {"path": "/Applications/BookText.app", "version": "0.1.0", "build": "1",
               "source_commit": None, "digest": "b" * 64}
        core = {"root": "/Core", "source_commit": "a" * 40, "tree": "c" * 40}
        with self.assertRaisesRegex(activation.ActivationError, "do not exactly match"):
            activation.build_event(
                registry=FakeRegistry(), state=state, app=app, core=core, target="target",
                native_reference="https://example.test/rollback",
                native_evidence={"status": "SUCCEEDED", "native_evidence": {"rollback": "verified"}},
                activated_at=None, recorded_at="2026-10-27T00:00:00+00:00",
                action="rollback", rollback_of="sha256:old",
            )

    def test_initial_registration_is_not_a_booktext_adapter_operation(self):
        state = {"products": {"booktext": {"current": None}}, "history": []}
        app = {"path": "/Applications/BookText.app", "version": "0.1.0", "build": "1",
               "source_commit": None, "digest": "b" * 64}
        core = {"root": "/Core", "source_commit": "a" * 40, "tree": "c" * 40}
        with self.assertRaisesRegex(activation.ActivationError, "initial registration is out of scope"):
            activation.build_event(
                registry=FakeRegistry(), state=state, app=app, core=core, target="local-primary",
                native_reference="https://example.test/activation",
                native_evidence={"status": "SUCCEEDED", "native_evidence": {"activation": "completed"}},
                activated_at=None, recorded_at="2026-10-27T00:00:00+00:00",
            )

    def test_core_source_identity_requires_a_clean_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Core"
            root.mkdir()
            (root / "booktext.py").write_text("pass\n", encoding="utf-8")
            activation._run("git", "init", str(root))
            activation._run("git", "-C", str(root), "config", "user.name", "BookText Test")
            activation._run("git", "-C", str(root), "config", "user.email", "booktext-test@example.invalid")
            activation._run("git", "-C", str(root), "add", "booktext.py")
            activation._run("git", "-C", str(root), "commit", "-m", "fixture")
            observed = activation.inspect_core(root / "booktext.py")
            self.assertEqual("booktext.py", Path(observed["script"]).name)
            self.assertEqual(40, len(observed["source_commit"]))
            self.assertEqual(40, len(observed["tree"]))
            (root / "dirty.txt").write_text("local", encoding="utf-8")
            with self.assertRaisesRegex(activation.ActivationError, "not clean"):
                activation.inspect_core(root / "booktext.py")

    def test_prepare_is_idempotent_and_preserves_existing_receipt_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            support = root / "support"
            native_reference = "https://example.test/native-activation"
            destination = support / hashlib.sha256(native_reference.encode("utf-8")).hexdigest()
            destination.mkdir(parents=True)
            (destination / "event.json").write_text(
                json.dumps({"product": "booktext", "event_id": "sha256:retained"}), encoding="utf-8"
            )
            (destination / "native-activation.json").write_text("retained proof", encoding="utf-8")
            before = {path.name: path.read_bytes() for path in destination.iterdir()}
            args = SimpleNamespace(support=support, native_reference=native_reference)
            with patch.object(activation, "inspect_app", side_effect=AssertionError("must not re-observe app")):
                result = activation.prepare(args)
            self.assertEqual("ALREADY_PREPARED", result["status"])
            self.assertEqual("sha256:retained", result["event_id"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in destination.iterdir()})

    def test_prepare_rejects_incomplete_existing_receipt_without_resealing(self):
        with tempfile.TemporaryDirectory() as temporary:
            support = Path(temporary) / "support"
            native_reference = "https://example.test/native-activation"
            destination = support / hashlib.sha256(native_reference.encode("utf-8")).hexdigest()
            destination.mkdir(parents=True)
            (destination / "event.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(activation.ActivationError, "incomplete activation receipt"):
                activation.prepare(SimpleNamespace(support=support, native_reference=native_reference))

    def test_stale_publication_keeps_exact_pending_receipt_for_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            event, proof, receipt = self._pending_receipt(Path(temporary))
            stale_state = {"products": {"booktext": {"current": "sha256:later"}}, "history": []}
            args = SimpleNamespace(receipt=receipt, registry_root=Path(temporary) / "registry")
            before = {path.name: path.read_bytes() for path in receipt.iterdir()}
            with patch.object(activation, "load_registry_module", return_value=(args.registry_root, FakeRegistry())), \
                 patch.object(activation, "read_remote_state", return_value=("head", {}, stale_state)), \
                 patch.object(activation, "_run", side_effect=AssertionError("stale receipt must not publish")):
                result = activation.publish(args)
            self.assertEqual("PUBLICATION_STALE", result["status"])
            self.assertEqual(event["event_id"], result["event_id"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in receipt.iterdir()})
            self.assertEqual(proof, (receipt / "native-activation.json").read_bytes())

    def test_publication_failure_keeps_exact_receipt_and_retry_observes_idempotence(self):
        with tempfile.TemporaryDirectory() as temporary:
            event, proof, receipt = self._pending_receipt(Path(temporary))
            args = SimpleNamespace(receipt=receipt, registry_root=Path(temporary) / "registry")
            pending_state = {"products": {"booktext": {"current": event["predecessor"]}}, "history": []}
            before = {path.name: path.read_bytes() for path in receipt.iterdir()}
            with patch.object(activation, "load_registry_module", return_value=(args.registry_root, FakeRegistry())), \
                 patch.object(activation, "read_remote_state", return_value=("head", {}, pending_state)), \
                 patch.object(activation, "_run", side_effect=activation.ActivationError("network unavailable")):
                result = activation.publish(args)
            self.assertEqual("PUBLICATION_PENDING", result["status"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in receipt.iterdir()})

            published_state = {"products": {"booktext": {"current": event["event_id"]}}, "history": [event]}
            with patch.object(activation, "load_registry_module", return_value=(args.registry_root, FakeRegistry())), \
                 patch.object(activation, "read_remote_state", return_value=("head", {}, published_state)):
                retried = activation.publish(args)
            self.assertEqual("ALREADY_PUBLISHED", retried["status"])
            self.assertEqual(event["event_id"], retried["current"])
            self.assertEqual(str(receipt.resolve()), retried["receipt"])


if __name__ == "__main__":
    unittest.main()
