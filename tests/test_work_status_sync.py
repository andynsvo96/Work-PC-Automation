import unittest
from datetime import datetime, timedelta, timezone

import server


def _payload(*, active=None, updated_at="2026-07-19T08:00:00", days=None, total=0.0):
    return {
        "success": True,
        "cap_hours": 45,
        "state": {
            "week_start": "2026-07-19",
            "total_paid_hours": total,
            "active_shift": active,
            "days": days or {},
            "updated_at": updated_at,
        },
    }


def _node(key, payload, *, seen_at, display_name=None):
    return {
        "node_key": key,
        "display_name": display_name or key,
        "enabled": True,
        "last_seen_at": seen_at.isoformat(),
        "runtime_status": {"work": payload},
    }


class SharedWorkStatusTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)

    def test_active_remote_shift_beats_newer_empty_local_file(self):
        local = _payload(updated_at="2026-07-19T10:00:00")
        remote = _payload(
            active={"date": "2026-07-19", "clock_in_at": "2026-07-19T07:34:00"},
            updated_at="2026-07-19T07:34:01",
            days={"2026-07-19": {"clock_in_at": "2026-07-19T07:34:00"}},
        )
        nodes = [_node("macbook", remote, seen_at=self.now, display_name="MacBook")]

        result = server._select_shared_work_status(local, nodes, local_node_key="windows", now=self.now)

        self.assertEqual(result["state"]["active_shift"]["clock_in_at"], "2026-07-19T07:34:00")
        self.assertEqual(result["shared_state"]["source_node"], "macbook")

    def test_later_clock_out_beats_stale_active_shift(self):
        active = _payload(
            active={"date": "2026-07-19", "clock_in_at": "2026-07-19T07:34:00"},
            days={"2026-07-19": {"clock_in_at": "2026-07-19T07:34:00"}},
        )
        closed = _payload(
            updated_at="2026-07-19T11:01:00",
            days={
                "2026-07-19": {
                    "clock_in_at": "2026-07-19T07:34:00",
                    "clock_out_at": "2026-07-19T11:00:00",
                }
            },
            total=3.43,
        )
        nodes = [_node("windows", closed, seen_at=self.now)]

        result = server._select_shared_work_status(active, nodes, local_node_key="macbook", now=self.now)

        self.assertIsNone(result["state"]["active_shift"])
        self.assertEqual(result["state"]["total_paid_hours"], 3.43)

    def test_offline_remote_state_is_ignored(self):
        local = _payload()
        remote = _payload(
            active={"date": "2026-07-19", "clock_in_at": "2026-07-19T07:34:00"},
            days={"2026-07-19": {"clock_in_at": "2026-07-19T07:34:00"}},
        )
        nodes = [_node("macbook", remote, seen_at=self.now - timedelta(minutes=2))]

        result = server._select_shared_work_status(local, nodes, local_node_key="windows", now=self.now)

        self.assertIsNone(result["state"]["active_shift"])
        self.assertEqual(result["shared_state"]["source_node"], "windows")


if __name__ == "__main__":
    unittest.main()
