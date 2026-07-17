import unittest
from unittest import mock

import safe_sync


class SafeSyncTests(unittest.TestCase):
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
