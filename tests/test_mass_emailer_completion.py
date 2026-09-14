import unittest
from unittest import mock

import server


class MassEmailerCompletionTests(unittest.TestCase):
    def run_worker(self, result):
        proc = mock.Mock(pid=12345, returncode=0)
        with (
            mock.patch.object(server.os.path, "exists", return_value=False),
            mock.patch.object(server.subprocess, "Popen", return_value=proc),
            mock.patch.object(server, "_automation_stop_is_blocking", return_value=False),
            mock.patch.object(server, "_automation_stop_requested_since", return_value=False),
            mock.patch.object(server, "_consume_force_stopped_pid", return_value=False),
            mock.patch.object(server, "_register_automation_process"),
            mock.patch.object(server, "_unregister_automation_process"),
            mock.patch.object(server, "_read_result_file_with_retry", return_value=result),
            mock.patch.object(server, "load_node_preferences", return_value={"headless": True}),
            mock.patch.object(server, "_resolve_console_python", return_value="python"),
            mock.patch("builtins.open", mock.mock_open()),
        ):
            return server._execute_crm_mass_emailer_worker(
                action="process_order", order_id="5212890",
                process="complicated_emb_to_hdd", dry_run=False,
            )

    def test_zero_exit_without_result_is_failure(self):
        ok, message, payload = self.run_worker(None)
        self.assertFalse(ok)
        self.assertFalse(payload["success"])
        self.assertTrue(payload["missing_result"])
        self.assertIn("without a completion result", message)

    def test_explicit_success_result_is_preserved(self):
        ok, message, payload = self.run_worker({
            "success": True, "message": "Email send confirmed", "order_id": "5212890",
        })
        self.assertTrue(ok)
        self.assertEqual(message, "Email send confirmed")
        self.assertEqual(payload["order_id"], "5212890")

    def test_explicit_failure_is_not_overridden_by_zero_exit(self):
        ok, message, payload = self.run_worker({
            "success": False, "message": "Salesforce verification expired",
        })
        self.assertFalse(ok)
        self.assertEqual(message, "Salesforce verification expired")


if __name__ == "__main__":
    unittest.main()
