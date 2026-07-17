import json
import os
import tempfile
import unittest

import node_preferences


class NodePreferencesTests(unittest.TestCase):
    def test_missing_file_preserves_safe_manual_default(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = node_preferences.load_node_preferences(os.path.join(folder, "missing.json"))
        self.assertEqual(payload, {"worker_mode": "manual", "manual_workers": 1})

    def test_update_persists_manual_override(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "node.json")
            payload = node_preferences.update_node_preferences(
                {"worker_mode": "manual", "manual_workers": 7},
                path,
            )
            self.assertEqual(payload["manual_workers"], 7)
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertEqual(stored, payload)

    def test_worker_count_is_bounded(self):
        self.assertEqual(node_preferences.normalize_node_preferences({"manual_workers": 99})["manual_workers"], 8)
        self.assertEqual(node_preferences.normalize_node_preferences({"manual_workers": 0})["manual_workers"], 1)


if __name__ == "__main__":
    unittest.main()
