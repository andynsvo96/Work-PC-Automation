import sys
import unittest
from pathlib import Path


WORKERS_DIR = Path(__file__).resolve().parents[1] / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import paycom_hours


class PaycomHoursTests(unittest.TestCase):
    def test_timesheet_split_shift_and_weekly_footer(self):
        import json
        import subprocess
        class Driver:
            def execute_script(self, script):
                table = [
                    ["Date", "Pay Code", "IN", "Allocation", "OUT", "IN", "Allocation", "OUT", "Hours", "Total Hours"],
                    ["Mon 09/07", "", "07:04 AM", "320", "09:37 AM", "12:11 PM", "320", "??", "2.55", "2.55"],
                    ["Sat 09/12", "", "", "", "", "", "", "", "", ""],
                    ["", "", "", "", "", "", "", "Weekly Totals", "12.18", ""],
                ]
                harness = "const rows = " + json.dumps(table) + ";" + """
const document = {querySelectorAll: () => [{getClientRects: () => [1],
  querySelectorAll: () => rows.map(row => ({querySelectorAll: () => row.map(text => ({innerText: text, textContent: text}))}))}]};
"""
                output = subprocess.check_output(["node", "-e", harness + "console.log(JSON.stringify((function(){" + script + "})()));"], text=True)
                return json.loads(output)
        rows = paycom_hours.extract_day_rows_from_timesheet(Driver())
        monday = next(row for row in rows if row["date_label"] == "Mon 09/07")
        self.assertEqual(monday["segments"], [{"clock_in": "07:04 AM", "clock_out": "09:37 AM"}, {"clock_in": "12:11 PM", "clock_out": None}])
        self.assertEqual(paycom_hours.extract_completed_week_hours_from_day_rows(rows), (2.55, 1, 1))
        self.assertIsNone(next(row for row in rows if row["date_label"] == "Sat 09/12")["hours"])

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
