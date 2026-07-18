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


if __name__ == "__main__":
    unittest.main()
