import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import slack_post_history


class SlackPostHistoryTests(unittest.TestCase):
    def test_history_returns_only_local_posts_from_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "slack_posts.jsonl"
            with mock.patch.object(slack_post_history, "SLACK_POST_HISTORY_FILE", str(history_path)):
                slack_post_history.record_slack_post(
                    channel_url="https://app.slack.com/client/T123/C456",
                    channel_name="#automation-team",
                    message="Starting the workday",
                    action="in",
                    posted_at=datetime(2026, 8, 7, 9, 5),
                )
                slack_post_history.record_slack_post(
                    channel_url="https://app.slack.com/client/T123/C456",
                    channel_name="automation-team",
                    message="Yesterday's message",
                    action="out",
                    posted_at=datetime(2026, 8, 6, 17, 0),
                )
                rows = slack_post_history.get_todays_slack_posts(now=datetime(2026, 8, 7, 12, 0))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel_name"], "automation-team")
        self.assertEqual(rows[0]["channel_id"], "C456")
        self.assertEqual(rows[0]["message"], "Starting the workday")
        self.assertEqual(rows[0]["posted_at"], "2026-08-07T09:05:00")


if __name__ == "__main__":
    unittest.main()
