import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKERS_DIR = ROOT / "workers"
for path in (ROOT, WORKERS_DIR):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

import rush_order_notifications  # noqa: E402


class RushOrderNotificationTests(unittest.TestCase):
    def test_exact_standard_international_rate_does_not_notify(self):
        result = rush_order_notifications.send_paid_rush_notification(
            "https://crm2.legacy.printfly.com/order/1234567",
            "Copyright",
            "$25.00",
        )

        self.assertFalse(result["eligible"])
        self.assertFalse(result["sent"])
        self.assertIn("international/military", result["reason"])

    def test_shipping_above_standard_international_rate_notifies(self):
        with mock.patch.object(
            rush_order_notifications,
            "_run_slack_team",
            return_value=(True, "sent"),
        ) as send:
            result = rush_order_notifications.send_paid_rush_notification(
                "https://crm2.legacy.printfly.com/order/1234567",
                "Shipping Issue",
                "$25.01",
            )

        self.assertTrue(result["eligible"])
        self.assertTrue(result["sent"])
        self.assertEqual(
            result["message"],
            "https://crm2.legacy.printfly.com/order/1234567 Rush Order - Shipping Issue",
        )
        send.assert_called_once_with(
            "custom",
            custom_message="https://crm2.legacy.printfly.com/order/1234567 Rush Order - Shipping Issue",
            channel_url=rush_order_notifications.RUSH_ORDER_SLACK_CHANNEL_URL,
        )

    def test_free_shipping_does_not_notify(self):
        result = rush_order_notifications.send_paid_rush_notification(
            "https://crm2.legacy.printfly.com/order/1234567",
            "Copyright",
            "Free",
        )

        self.assertFalse(result["eligible"])
        self.assertFalse(result["sent"])


if __name__ == "__main__":
    unittest.main()
