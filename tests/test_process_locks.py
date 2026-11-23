"""Exercise the shared cart lock with local processes, never vendor browsers."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from process_locks import exclusive_process_lock
from runtime_paths import DEBUG_DIR


class ProcessLockTests(unittest.TestCase):
    def setUp(self):
        Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="cart-lock-", dir=DEBUG_DIR)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "cart.lock"

    def run_child(self, code):
        return subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True,
                              text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_other_process_cannot_enter_until_lock_released(self):
        code = "from process_locks import exclusive_process_lock\nimport sys\ntry:\n with exclusive_process_lock(sys.argv[1], timeout=0.2): print('acquired')\nexcept TimeoutError: print('blocked')"
        with exclusive_process_lock(self.path):
            result = self.run_child(code)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "blocked")
        self.assertEqual(self.run_child(code).stdout.strip(), "acquired")

    def test_process_exit_releases_lock_without_deleting_lock_file(self):
        ready = self.path.with_suffix(".ready")
        code = "from process_locks import exclusive_process_lock\nfrom pathlib import Path\nimport sys\nwith exclusive_process_lock(sys.argv[1]):\n Path(sys.argv[1]).with_suffix('.ready').write_text('ready')\n sys.stdin.read()"
        child = subprocess.Popen([sys.executable, "-c", code, str(self.path)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "Lock holder did not start")
            child.kill()
            child.wait(timeout=5)
            with exclusive_process_lock(self.path, timeout=0.3):
                self.assertTrue(self.path.exists())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
