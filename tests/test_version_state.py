import os
import unittest

import version_state


class VersionStateTests(unittest.TestCase):
    def test_current_repository_state_is_readable(self):
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        payload = version_state.get_git_version_state(repo_dir)
        self.assertTrue(payload["available"], payload.get("error"))
        self.assertTrue(payload["commit"])
        self.assertEqual(payload["queue_protocol_version"], version_state.QUEUE_PROTOCOL_VERSION)
        self.assertIsInstance(payload["dirty"], bool)


if __name__ == "__main__":
    unittest.main()
