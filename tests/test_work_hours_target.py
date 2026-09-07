import copy
import unittest
from datetime import datetime
from unittest import mock

import server


class WorkHoursTargetTests(unittest.TestCase):
    def preview(self, scope, target, now=None, today=0, week=0, clock_in="2026-09-07T08:00:00"):
        state = {
            "total_paid_hours": week,
            "active_shift": {"date": "2026-09-07", "clock_in_at": clock_in,
                             "auto_clock_out_at": "2026-09-07T18:00:00"},
            "days": {"2026-09-07": {"paid_hours": today}},
        }
        before = copy.deepcopy(state)
        with (
            mock.patch.object(server, "datetime", wraps=datetime) as clock,
            mock.patch.object(server, "load_work_state", return_value=state),
            mock.patch.object(server, "WORK_CLOCK_BREAK_MINUTES", 30),
            mock.patch.object(server, "WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS", 4),
            mock.patch.object(server, "_max_auto_clock_out_horizon_hours", return_value=16),
            mock.patch.object(server, "save_work_state") as save,
            mock.patch.object(server, "schedule_auto_clock_out") as schedule,
        ):
            clock.now.return_value = now or datetime(2026, 9, 7, 13, 8)
            result = server.preview_work_hours_target(scope, target)
            save.assert_not_called()
            schedule.assert_not_called()
        self.assertEqual(state, before)
        return result

    def test_today_six_paid_hours_requires_six_and_half_elapsed(self):
        result = self.preview("today", 6)
        self.assertTrue(result["success"])
        self.assertEqual(result["scheduled_for"], "2026-09-07T14:30:00")
        self.assertEqual(result["remaining_minutes"], 82)
        self.assertEqual(result["break_minutes"], 30)

    def test_week_target_uses_saved_weekly_hours(self):
        result = self.preview("week", 35, week=29)
        self.assertEqual(result["scheduled_for"], "2026-09-07T14:30:00")

    def test_today_includes_completed_split_shift(self):
        result = self.preview("today", 6, today=2, week=29, now=datetime(2026, 9, 7, 13), clock_in="2026-09-07T12:00:00")
        self.assertEqual(result["scheduled_for"], "2026-09-07T16:00:00")
        self.assertEqual(result["break_minutes"], 0)

    def test_threshold_and_second_crossing_after_break(self):
        self.assertEqual(self.preview("today", 4, now=datetime(2026, 9, 7, 11))["scheduled_for"], "2026-09-07T12:00:00")
        self.assertEqual(self.preview("today", 4, now=datetime(2026, 9, 7, 12, 10))["scheduled_for"], "2026-09-07T12:30:00")

    def test_invalid_or_reached_targets(self):
        for scope, target in [("today", 4), ("week", 0), ("today", "bad"), ("today", float("nan")), ("week", float("inf")), ("elapsed", 6), ("today", 100)]:
            with self.subTest(scope=scope, target=target):
                self.assertFalse(self.preview(scope, target)["success"])

    def test_stale_scheduled_shift_never_punches(self):
        with (
            mock.patch.object(server, "load_work_state", return_value={"active_shift": {"clock_in_at": "different"}}),
            mock.patch.object(server, "_run_clock_action") as punch,
            mock.patch.object(server, "_audit_result"),
        ):
            ok, message = server.run_work("out", expected_clock_in_at="original")
        self.assertFalse(ok)
        self.assertIn("original shift", message)
        punch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
