import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import server


class WorkSyncTests(unittest.TestCase):
    def test_auto_clock_out_uses_a_local_timer(self):
        auto_out_at = datetime.now() + timedelta(hours=1)
        timer = mock.Mock()
        with (
            mock.patch.object(server, "auto_clock_timer", None),
            mock.patch.object(server.threading, "Timer", return_value=timer) as timer_factory,
        ):
            server.schedule_auto_clock_out(auto_out_at)

        delay = timer_factory.call_args.args[0]
        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, 3600)
        self.assertIs(timer_factory.call_args.args[1], server._auto_clock_out_timer_callback)
        self.assertTrue(timer.daemon)
        timer.start.assert_called_once_with()

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
