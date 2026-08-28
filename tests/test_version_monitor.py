import unittest
from unittest import mock

import server


class VersionMonitorTests(unittest.TestCase):
    def test_automatic_updates_default_to_enabled_and_honor_opt_out(self):
        with mock.patch.object(server.config_module, "AUTOMATION_AUTO_UPDATE_ENABLED", True, create=True):
            self.assertTrue(server._automatic_app_updates_enabled())
        with mock.patch.object(server.config_module, "AUTOMATION_AUTO_UPDATE_ENABLED", "off", create=True):
            self.assertFalse(server._automatic_app_updates_enabled())

    def test_start_version_monitor_starts_one_daemon_thread(self):
        monitor = mock.Mock()
        monitor.is_alive.return_value = False
        stop_event = mock.Mock()
        with (
            mock.patch.object(server.config_module, "AUTOMATION_AUTO_UPDATE_ENABLED", True, create=True),
            mock.patch.object(server, "version_monitor_thread", None),
            mock.patch.object(server, "version_monitor_stop", stop_event),
            mock.patch.object(server.threading, "Thread", return_value=monitor) as thread,
        ):
            self.assertIs(server.start_version_monitor(), monitor)

        stop_event.clear.assert_called_once_with()
        thread.assert_called_once_with(
            target=server._version_monitor_loop,
            name="automation-version-monitor",
            daemon=True,
        )
        monitor.start.assert_called_once_with()

    def test_start_version_monitor_does_not_duplicate_a_live_monitor(self):
        monitor = mock.Mock()
        monitor.is_alive.return_value = True
        with (
            mock.patch.object(server.config_module, "AUTOMATION_AUTO_UPDATE_ENABLED", True, create=True),
            mock.patch.object(server, "version_monitor_thread", monitor),
            mock.patch.object(server.threading, "Thread") as thread,
        ):
            self.assertIs(server.start_version_monitor(), monitor)

        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
