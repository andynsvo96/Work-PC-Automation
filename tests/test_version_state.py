import os
import subprocess
import unittest
from unittest import mock

import version_state


class VersionStateTests(unittest.TestCase):
    def test_git_uses_background_creation_flags_when_available(self):
        completed = subprocess.CompletedProcess(["git"], 0, stdout="abc\n", stderr="")
        with mock.patch("version_state.subprocess.run", return_value=completed) as run:
            self.assertEqual(version_state._git(".", "rev-parse", "HEAD"), "abc")

        if version_state._GIT_CREATION_FLAGS:
            self.assertEqual(
                run.call_args.kwargs.get("creationflags"),
                version_state._GIT_CREATION_FLAGS,
            )
        else:
            self.assertNotIn("creationflags", run.call_args.kwargs)

    def test_current_repository_state_is_readable(self):
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        payload = version_state.get_git_version_state(repo_dir)
        self.assertTrue(payload["available"], payload.get("error"))
        self.assertTrue(payload["commit"])
        self.assertEqual(payload["queue_protocol_version"], version_state.QUEUE_PROTOCOL_VERSION)
        self.assertIsInstance(payload["dirty"], bool)

    def test_refresh_origin_main_fetches_without_changing_worktree(self):
        with mock.patch("version_state._git") as git:
            with mock.patch("version_state.get_git_version_state", return_value={"relation": "current"}) as state:
                payload = version_state.refresh_origin_main(".", timeout=17)

        fetch_call = git.call_args
        self.assertEqual(fetch_call.args[1:], ("fetch", "--quiet", "--prune", "origin"))
        self.assertEqual(fetch_call.kwargs["timeout"], 17)
        state.assert_called_once()
        self.assertEqual(payload, {"relation": "current"})


if __name__ == "__main__":
    unittest.main()
