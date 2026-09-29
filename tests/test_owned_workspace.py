import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.owned_workspace import MARKER, OwnedWorkspace, reap_explicit


class OwnedWorkspaceTests(unittest.TestCase):
    def test_creator_removes_only_its_marked_workspace_and_is_idempotent(self):
        workspace = OwnedWorkspace()
        path = workspace.path
        marker = json.loads((path / MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker["workspace_id"], workspace.workspace_id)
        self.assertTrue(workspace.close())
        self.assertFalse(path.exists())
        self.assertFalse(workspace.close())

    def test_explicit_reaper_refuses_unowned_and_live_workspaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unrelated-booktext-looking-name"
            path.mkdir()
            self.assertEqual(reap_explicit(path, workspace_id="anything")["reason"], "unowned_or_marker_mismatch")
            self.assertTrue(path.exists())
        workspace = OwnedWorkspace()
        try:
            self.assertEqual(reap_explicit(workspace.path, workspace_id=workspace.workspace_id)["reason"], "owner_process_is_live")
            self.assertTrue(workspace.path.exists())
        finally:
            workspace.close()

    def test_explicit_reaper_requires_exact_marker_and_dead_owner(self):
        workspace = OwnedWorkspace()
        path, workspace_id = workspace.path, workspace.workspace_id
        workspace._closed = True  # simulate an interrupted creator; do not call close
        with mock.patch("tools.owned_workspace._pid_alive", return_value=False):
            self.assertEqual(reap_explicit(path, workspace_id=workspace_id), {"reaped": True, "reason": "explicit_stale_owned_workspace"})
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
