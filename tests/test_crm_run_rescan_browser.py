"""Offline Push Back report scanning with an isolated Chrome profile."""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selenium import webdriver
from selenium.webdriver.chrome.service import Service


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
import crm_push_back as worker


class PushBackReportScanBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                driver_path = str(max(candidates, key=lambda path: next(
                    (tuple(map(int, part.split('.'))) for part in path.parts if re.fullmatch(r"\d+(?:\.\d+){3}", part)), ()
                )))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver for offline Push Back report verification.")
        artifacts = ROOT / "runtime/debug/run_rescan_tests"
        artifacts.mkdir(parents=True, exist_ok=True)
        profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=artifacts)
        cls.addClassCleanup(profile.cleanup)
        options = webdriver.ChromeOptions()
        for flag in ("--headless=new", "--disable-background-networking", "--disable-sync", "--no-sandbox", "--no-first-run"):
            options.add_argument(flag)
        options.add_argument(f"--user-data-dir={profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)

    def test_eligible_purple_rows_below_the_raw_limit_are_found_and_limited_after_filtering(self):
        fixture = (ROOT / "tests/fixtures/crm_push_back_list.html").as_uri()
        for mode in ("rush", "free", "all"):
            for limit, excluded, expected in (
                (8, set(), ["5000043", "5000044"]),
                (1, set(), ["5000043"]),
                (1, {"5000043"}, ["5000044"]),
            ):
                with self.subTest(mode=mode, limit=limit, excluded=excluded):
                    self.driver.get(fixture)
                    with mock.patch.object(worker, "safe_get_with_partial_load"), \
                         mock.patch.object(worker, "login_if_needed", return_value=False), \
                         mock.patch.object(worker, "_publish_status"):
                        rows = worker._collect_push_back_rows_with_driver(
                            self.driver, mode, limit, fixture, exclude_order_ids=excluded,
                        )
                    self.assertEqual([row["orderId"] for row in rows], expected)
                    self.assertTrue(all(row["colorLabel"] == "purple" for row in rows))


if __name__ == "__main__":
    unittest.main()
