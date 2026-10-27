import hashlib
import tempfile
import unittest
from pathlib import Path

from tools import activation_publication as activation


class FakeRegistry:
    def subject_hash(self, value):
        return "subject-hash"

    def encode(self, value):
        import json
        return (json.dumps(value, sort_keys=True) + "\n").encode()

    def sha256(self, value):
        return hashlib.sha256(value).hexdigest()

    def event_id(self, value):
        return "sha256:event"


class ActivationPublicationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
