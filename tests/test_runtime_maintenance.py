import os
import tempfile
import time
import unittest
from pathlib import Path

from runtime_maintenance import prune_screenshot_artifacts


class RuntimeMaintenanceTests(unittest.TestCase):
    def test_prunes_old_and_excess_screenshots_but_preserves_other_files(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "screenshot_old.png"
            recent = root / "screenshot_recent.png"
            newest = root / "screenshot_newest.txt"
            preserved = root / "result.json"
            for path in (old, recent, newest, preserved):
                path.write_text(path.name, encoding="utf-8")
            os.utime(old, (now - 40 * 86400, now - 40 * 86400))
            os.utime(recent, (now - 20, now - 20))
            os.utime(newest, (now - 10, now - 10))

            result = prune_screenshot_artifacts(
                folder,
                max_age_days=30,
                max_files=1,
                now=now,
            )

            self.assertEqual(result["removed_files"], 2)
            self.assertFalse(old.exists())
            self.assertFalse(recent.exists())
            self.assertTrue(newest.exists())
            self.assertTrue(preserved.exists())

    def test_ignores_nested_directories_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nested = root / "nested"
            nested.mkdir()
            nested_file = nested / "screenshot_nested.png"
            nested_file.write_text("keep", encoding="utf-8")
            link = root / "screenshot_link.png"
            try:
                link.symlink_to(nested_file)
            except OSError:
                link = None

            result = prune_screenshot_artifacts(folder, max_age_days=0, max_files=0)

            self.assertEqual(result["removed_files"], 0)
            self.assertTrue(nested_file.exists())
            if link is not None:
                self.assertTrue(link.exists())


if __name__ == "__main__":
    unittest.main()
