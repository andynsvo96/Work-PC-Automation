import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import server


class WorkSyncTests(unittest.TestCase):
    def test_shared_auto_clock_out_is_queued_for_any_node_but_prefers_its_origin(self):
        fd, state_path = tempfile.mkstemp(prefix="work-auto-failover-", suffix=".json")
        os.close(fd)
        auto_out_at = datetime.now() + timedelta(hours=1)

        class _Config:
            node_key = "windows-pc"

        class _Client:
            config = _Config()

        class _Runtime:
            client = _Client()

        captured = {}

        def enqueue(*args, **kwargs):
            captured["args"] = args
            captured.update(kwargs)
            return True, "Queued", {"id": "shared-auto-out-1"}

        try:
            with (
                mock.patch.object(server, "WORK_STATE_FILE", state_path),
                mock.patch.object(server, "AUTOMATION_QUEUE_MODE", "shared"),
                mock.patch.object(server, "shared_queue_runtime", _Runtime()),
                mock.patch.object(server, "enqueue_automation", side_effect=enqueue),
            ):
                state = server._new_work_state(auto_out_at.date())
                state["active_shift"] = {
                    "date": auto_out_at.date().isoformat(),
                    "clock_in_at": datetime.now().isoformat(),
                    "auto_clock_out_at": auto_out_at.isoformat(),
                }
                server.save_work_state(state)

                ok, _message = server.schedule_auto_clock_out(auto_out_at)
                saved = server.load_work_state(auto_out_at)

            self.assertTrue(ok)
            self.assertTrue(captured["allow_any_node"])
            self.assertEqual(captured["preferred_node"], "windows-pc")
            self.assertEqual(captured["task_arguments"]["origin_node"], "windows-pc")
            self.assertEqual(saved["active_shift"]["auto_clock_out_queue_task_id"], "shared-auto-out-1")
        finally:
            if os.path.exists(state_path):
                os.remove(state_path)

    def test_manual_sync_imports_open_paycom_punch_and_recomputes_cap_schedule(self):
        today = datetime.now().date()
        day_label = today.strftime("%a %m/%d")
        fd, state_path = tempfile.mkstemp(prefix="work-sync-", suffix=".json")
        os.close(fd)
        try:
            with (
                mock.patch.object(server, "WORK_STATE_FILE", state_path),
                mock.patch.object(server, "WORK_CLOCK_SYNC_FROM_PAYCOM", True),
                mock.patch.object(server, "WORK_CLOCK_CAPPED", True),
                mock.patch.object(
                    server,
                    "sync_week_hours_from_paycom",
                    return_value=(
                        True,
                        "Paycom sync succeeded.",
                        38.46,
                        [
                            {
                                "date_label": day_label,
                                "hours": None,
                                "clock_in": "07:41 AM",
                                "clock_out": None,
                            }
                        ],
                    ),
                ),
                mock.patch.object(
                    server,
                    "ensure_auto_clock_out_schedule_if_needed",
                    return_value=(True, "Auto clock-out is now scheduled for today 2:43 PM."),
                ) as schedule,
            ):
                server.save_work_state(server._new_work_state(today))

                ok, message = server.run_work_sync()

                state = server.load_work_state(datetime.combine(today, datetime.min.time()))

            self.assertTrue(ok)
            self.assertEqual(state["total_paid_hours"], 38.46)
            self.assertEqual(state["active_shift"]["source"], "paycom-sync")
            self.assertIn("Detected active Paycom clock-in", message)
            schedule.assert_called_once_with(force_recompute=True)
        finally:
            if os.path.exists(state_path):
                os.remove(state_path)


if __name__ == "__main__":
    unittest.main()
