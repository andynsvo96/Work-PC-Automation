import os
import types
import unittest
from unittest import mock

import server


class ServerLifecycleTests(unittest.TestCase):
    def test_windows_existing_server_termination_preserves_replacement_descendants(self):
        connection = types.SimpleNamespace(
            pid=123,
            status="LISTEN",
            laddr=types.SimpleNamespace(port=5123),
        )
        process = mock.Mock(pid=123)
        process.cmdline.return_value = ["pythonw.exe", "server.py"]
        process.cwd.return_value = server.SCRIPT_DIR
        fake_psutil = types.SimpleNamespace(
            CONN_LISTEN="LISTEN",
            net_connections=mock.Mock(side_effect=[[connection], []]),
            Process=mock.Mock(return_value=process),
            pid_exists=mock.Mock(return_value=False),
        )
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(server.os, "name", "nt"),
            mock.patch.object(server.os, "getpid", return_value=999),
            mock.patch.object(server.subprocess, "run", return_value=completed) as run,
            mock.patch.dict(server.sys.modules, {"psutil": fake_psutil}),
        ):
            stopped = server.kill_existing_server(5123)

        self.assertTrue(stopped)
        run.assert_called_once_with(
            ["taskkill", "/F", "/PID", "123"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(server.subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_existing_server_failure_is_fail_closed(self):
        connection = types.SimpleNamespace(
            pid=123,
            status="LISTEN",
            laddr=types.SimpleNamespace(port=5123),
        )
        process = mock.Mock(pid=123)
        process.cmdline.return_value = ["pythonw.exe", "server.py"]
        process.cwd.return_value = server.SCRIPT_DIR
        fake_psutil = types.SimpleNamespace(
            CONN_LISTEN="LISTEN",
            net_connections=mock.Mock(return_value=[connection]),
            Process=mock.Mock(return_value=process),
            pid_exists=mock.Mock(return_value=True),
        )
        failed = types.SimpleNamespace(returncode=1, stdout="", stderr="access denied")

        with (
            mock.patch.object(server.os, "name", "nt"),
            mock.patch.object(server.os, "getpid", return_value=999),
            mock.patch.object(server.subprocess, "run", return_value=failed),
            mock.patch.dict(server.sys.modules, {"psutil": fake_psutil}),
        ):
            stopped = server.kill_existing_server(5123)

        self.assertFalse(stopped)

    def test_server_identity_rejects_same_filename_from_another_workspace(self):
        process = mock.Mock()
        process.cmdline.return_value = ["pythonw.exe", "server.py"]
        process.cwd.return_value = os.path.join(server.SCRIPT_DIR, "unrelated")

        self.assertFalse(server._process_runs_this_server(process))

    def test_macos_restart_uses_loaded_launch_agent(self):
        probe = types.SimpleNamespace(returncode=0)
        with (
            mock.patch.object(server.os, "name", "posix"),
            mock.patch.object(server.sys, "platform", "darwin"),
            mock.patch.object(server.os, "getuid", return_value=501, create=True),
            mock.patch.object(server.subprocess, "run", return_value=probe) as run,
            mock.patch.object(server.subprocess, "Popen") as popen,
        ):
            ok, message = server._restart_server_process()

        self.assertTrue(ok)
        self.assertIn("LaunchAgent", message)
        run.assert_called_once_with(
            ["launchctl", "print", "gui/501/com.workautomation.server"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(
            popen.call_args.args[0],
            ["launchctl", "kickstart", "-k", "gui/501/com.workautomation.server"],
        )

    def test_macos_orphan_cleanup_targets_only_matching_server_processes(self):
        matching = mock.Mock(pid=100, info={"cmdline": ["python", server.__file__]})
        unrelated = mock.Mock(pid=101, info={"cmdline": ["python", "/tmp/server.py"]})
        fake_psutil = types.SimpleNamespace(
            process_iter=mock.Mock(return_value=[matching, unrelated]),
            wait_procs=mock.Mock(return_value=([matching], [])),
        )
        with (
            mock.patch.object(server.sys, "platform", "darwin"),
            mock.patch.object(server.os, "getpid", return_value=999),
            mock.patch.dict(server.sys.modules, {"psutil": fake_psutil}),
        ):
            stopped = server._stop_orphan_mac_server_processes()

        self.assertEqual(stopped, 1)
        matching.terminate.assert_called_once_with()
        unrelated.terminate.assert_not_called()



if __name__ == "__main__":
    unittest.main()
