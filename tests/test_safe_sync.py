import subprocess
import unittest
from unittest import mock

import safe_sync


class SafeSyncTests(unittest.TestCase):
    def test_status_output_is_safe_without_a_console(self):
        with mock.patch.object(safe_sync.sys, "stdout", None):
            safe_sync._write_status("hidden launcher")

    def test_git_uses_background_creation_flags_when_available(self):
        completed = subprocess.CompletedProcess(["git"], 0, stdout="ok\n", stderr="")
        with mock.patch("safe_sync.subprocess.run", return_value=completed) as run:
            self.assertEqual(safe_sync._run(".", "status"), (0, "ok"))

        if safe_sync._GIT_CREATION_FLAGS:
            self.assertEqual(
                run.call_args.kwargs.get("creationflags"),
                safe_sync._GIT_CREATION_FLAGS,
            )
        else:
            self.assertNotIn("creationflags", run.call_args.kwargs)

    @mock.patch("safe_sync.get_git_version_state")
    def test_dirty_tree_is_blocked_without_fetch(self, mock_state):
        mock_state.return_value = {"available": True, "dirty": True, "relation": "current"}
        with mock.patch("safe_sync._run") as mock_run:
            result = safe_sync.sync_repository(".")
        self.assertTrue(result["blocked"])
        mock_run.assert_not_called()

    @mock.patch("safe_sync.get_git_version_state")
    def test_current_tree_does_not_pull(self, mock_state):
        mock_state.side_effect = [
            {"available": True, "dirty": False, "relation": "current"},
            {"available": True, "dirty": False, "relation": "current"},
        ]
        with mock.patch("safe_sync._run", return_value=(0, "")) as mock_run:
            result = safe_sync.sync_repository(".")
        self.assertTrue(result["success"])
        self.assertFalse(result["updated"])
        self.assertEqual(mock_run.call_args.args[1:], ("fetch", "origin", "main"))

    @mock.patch("safe_sync.get_git_version_state")
    def test_behind_tree_uses_fast_forward_only(self, mock_state):
        mock_state.side_effect = [
            {"available": True, "dirty": False, "relation": "behind"},
            {"available": True, "dirty": False, "relation": "behind"},
            {"available": True, "dirty": False, "relation": "current"},
        ]
        with mock.patch("safe_sync._run", side_effect=[(0, ""), (0, "updated")]) as mock_run:
            result = safe_sync.sync_repository(".")
        self.assertTrue(result["updated"])
        self.assertEqual(mock_run.call_args_list[1].args[1:], ("pull", "--ff-only", "origin", "main"))


if __name__ == "__main__":
    unittest.main()
