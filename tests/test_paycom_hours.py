import sys
import unittest
from pathlib import Path


WORKERS_DIR = Path(__file__).resolve().parents[1] / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import paycom_hours


class PaycomHoursTests(unittest.TestCase):
    def test_open_shift_uses_zero_completed_hours_baseline(self):
        rows = [
            {
                "date_label": "Sun 08/09",
                "hours": None,
                "clock_in": "08:36 AM",
                "clock_out": None,
            },
            {"date_label": "Mon 08/10", "hours": None, "clock_in": None, "clock_out": None},
        ]

        hours, numeric_days, open_shifts = paycom_hours.extract_completed_week_hours_from_day_rows(rows)

        self.assertEqual(hours, 0.0)
        self.assertEqual(numeric_days, 0)
        self.assertEqual(open_shifts, 1)

    def test_completed_daily_totals_are_summed_but_open_shift_is_not_estimated(self):
        rows = [
            {"date_label": "Sun 08/09", "hours": 7.75, "clock_in": "08:00 AM", "clock_out": "04:15 PM"},
            {"date_label": "Mon 08/10", "hours": 8.25, "clock_in": "08:05 AM", "clock_out": "04:50 PM"},
            {"date_label": "Tue 08/11", "hours": None, "clock_in": "08:36 AM", "clock_out": None},
        ]

        hours, numeric_days, open_shifts = paycom_hours.extract_completed_week_hours_from_day_rows(rows)

        self.assertEqual(hours, 16.0)
        self.assertEqual(numeric_days, 2)
        self.assertEqual(open_shifts, 1)

    def test_unrecognized_rows_do_not_turn_a_parser_failure_into_zero_hours(self):
        rows = [{"date_label": "Weekly Totals", "hours": 40.0}]

        hours, numeric_days, open_shifts = paycom_hours.extract_completed_week_hours_from_day_rows(rows)

        self.assertIsNone(hours)
        self.assertEqual(numeric_days, 0)
        self.assertEqual(open_shifts, 0)


if __name__ == "__main__":
    unittest.main()
