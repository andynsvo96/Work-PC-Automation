"""Run Order Goods only against a local fixture and isolated Chrome profile."""

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
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_order_goods as worker  # noqa: E402
import crm_order_goods_native as native  # noqa: E402
import crm_order_goods_only as only  # noqa: E402


class StockOrderingBrowserTests(unittest.TestCase):
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
            raise unittest.SkipTest("No local ChromeDriver for offline Order Goods verification.")
        artifact_dir = ROOT / "runtime/debug/order_goods_tests"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=artifact_dir)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for flag in ("--headless=new", "--disable-background-networking", "--disable-sync", "--disable-gpu", "--no-sandbox", "--no-first-run", "--window-size=1200,900"):
            options.add_argument(flag)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)
        cls.fixture_url = (ROOT / "tests/fixtures/crm_order_goods_tabs.html").as_uri()

    def setUp(self):
        self.driver.get(self.fixture_url)
        self.driver.execute_script("sessionStorage.clear()")
        self.driver.refresh()
        status = mock.patch.object(worker, "_publish_status")
        status.start()
        self.addCleanup(status.stop)
        receipts = tempfile.TemporaryDirectory(prefix="receipts-", dir=ROOT / "runtime/debug/order_goods_tests")
        self.addCleanup(receipts.cleanup)
        directory = mock.patch.object(native, "RECEIPT_DIR", Path(receipts.name))
        directory.start()
        self.addCleanup(directory.stop)

    def test_finds_all_designs_with_view_proofs_and_duplicate_labels(self):
        tabs = worker._find_stock_tabs(self.driver)
        self.assertEqual(len(tabs), 4)
        self.assertIn("View Proofs", tabs[1]["label"])
        self.assertEqual(tabs[2]["label"], tabs[3]["label"])

    def test_ordering_only_submits_without_refreshing_or_reading_error_messages(self):
        self.driver.execute_script("""
          nativeModes[3]='fail';
          const submit=inventoryService.orderGoodsSupplynet;
          inventoryService.orderGoodsSupplynet=id=>submit(id).then(response=>{
            Object.defineProperty(response,'errors',{get(){throw Error('Supplier errors must not be read');}});
            return response;
          });
        """)
        with mock.patch.object(self.driver, "refresh", wraps=self.driver.refresh) as refresh, \
             mock.patch.object(worker, "_unlock_current_order_for_auto_ordering") as unlock, \
             mock.patch.object(worker, "_wait_for_auto_order_feedback") as feedback:
            results = only.order_window(worker, self.driver, ["5000001"], existing_handle=self.driver.current_window_handle)
        self.assertEqual([row["outcome"] for row in results], ["order_goods_skipped"] + ["order_goods_submitted"] * 3)
        self.assertTrue(all(row["success"] for row in results))
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4])
        self.assertFalse(any("Not enough stock" in row["message"] for row in results))
        refresh.assert_not_called(); unlock.assert_not_called(); feedback.assert_not_called()
        self.assertTrue(native._load("5000001")["submissionOnly"])
        self.assertIsNone(native.recovery_block("5000001", submission_only=True))

    def test_ordering_only_skips_locked_stock_and_dry_run_never_submits(self):
        self.driver.execute_script("fixtureLocked=true;")
        results = only.order_window(worker, self.driver, ["5000001"], existing_handle=self.driver.current_window_handle)
        self.assertTrue(all(row["success"] and row["outcome"] == "order_goods_skipped" for row in results))
        self.driver.execute_script("fixtureLocked=false;")
        results = only.order_window(worker, self.driver, ["5000001"], dry_run=True, existing_handle=self.driver.current_window_handle)
        self.assertEqual(sum(row["outcome"] == "order_goods_ready" for row in results), 3)
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [])
        self.assertEqual(list(native.RECEIPT_DIR.iterdir()), [])

    def test_ordering_only_supports_one_product_without_legacy_button_clicks(self):
        self.driver.execute_script("""
          resource.designs=[resource.designs[3]];
          window.submittedDesigns=[];
          inventoryService.orderGoodsSupplynet=id=>{
            submittedDesigns.push(String(id));
            return Promise.resolve({succeeded:true,supplier:'Sanmar'});
          };
        """)
        results = only.order_window(worker, self.driver, ["5000001"], existing_handle=self.driver.current_window_handle)
        self.assertEqual([row["outcome"] for row in results], ["order_goods_submitted"])
        self.assertEqual(self.driver.execute_script("return submittedDesigns"), ["104"])
        self.assertEqual(self.driver.execute_script("return stockFixtureState().legacyClicks"), 0)
        repeated = only.order_window(worker, self.driver, ["5000001"], existing_handle=self.driver.current_window_handle)
        self.assertEqual(repeated[0]["outcome"], "order_goods_skipped")
        self.assertEqual(self.driver.execute_script("return submittedDesigns"), ["104"])

    def test_ordering_only_loads_two_background_orders_in_one_browser_before_submission(self):
        url = self.fixture_url
        events = []
        original = self.driver.execute_cdp_cmd
        def create(command, options):
            if command == "Target.createTarget":
                events.append("open")
            return original(command, options)
        read = only._read
        def observe(*args, **kwargs):
            events.append(args[2] if len(args) > 2 else "inspect")
            return read(*args, **kwargs)
        source = self.driver.current_window_handle
        with mock.patch.object(self.driver, "execute_cdp_cmd", side_effect=create), mock.patch.object(only, "_read", side_effect=observe):
            results = only.order_window(worker, self.driver, ["5000001", "5000002"], order_url=lambda value: url + "?order=" + value)
        self.assertEqual(events[:2], ["open", "open"])
        self.assertLess(max(i for i, event in enumerate(events) if event == "start"), min(i for i, event in enumerate(events) if event == "status"))
        self.assertEqual(sum(row["outcome"] == "order_goods_submitted" for row in results), 6)
        self.assertEqual(self.driver.window_handles, [source])

    def test_po_detection_uses_visible_records_and_ignores_notes_and_drafts(self):
        for layout in ("table", "grid"):
            with self.subTest(layout=layout):
                self.driver.get(self.fixture_url + "?layout=" + layout)
                for index, expected in enumerate((True, True, False, False)):
                    self.assertIsNotNone(worker._activate_stock_tab(self.driver, index))
                    self.assertEqual(worker._active_stock_tab_has_purchase_order(self.driver), expected)

    def test_orders_remaining_tabs_and_revisits_new_sizes_on_a_later_run(self):
        with mock.patch.object(worker, "_require_order_goods_page_ready"), \
             mock.patch.object(worker, "_activate_stock_tab") as activate, \
             mock.patch.object(self.driver, "refresh", wraps=self.driver.refresh) as refresh:
            results = worker._order_goods_for_all_stock_tabs(self.driver, "5000001")
            self.assertEqual([item["outcome"] for item in results], [
                "already_stock_ordered", "auto_order_succeeded", "auto_order_succeeded", "auto_order_succeeded",
            ])
            self.assertEqual([item["stock_tab_index"] for item in results], [1, 2, 3, 4])
            self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4])
            state = self.driver.execute_script("return stockFixtureState()")
            self.assertEqual(state["events"][:3], ["start:2", "start:3", "start:4"])
            self.assertEqual(state["legacyClicks"], 0)
            activate.assert_not_called()
            self.assertEqual(refresh.call_count, 1)

            self.driver.execute_script("addSizeToDesign(4)")
            results = worker._order_goods_for_all_stock_tabs(self.driver, "5000001")
            self.assertEqual([item["outcome"] for item in results], [
                "already_stock_ordered", "already_stock_ordered", "already_stock_ordered", "auto_order_succeeded",
            ])
            self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4, 4])

    def test_dry_run_checks_all_tabs_without_clicking_order_goods(self):
        results = worker._order_goods_for_all_stock_tabs(self.driver, "5000001", dry_run=True)
        self.assertEqual([item["outcome"] for item in results], [
            "already_stock_ordered", "order_goods_ready", "order_goods_ready", "order_goods_ready",
        ])
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [])

    def order_native(self):
        with mock.patch.object(worker, "_require_order_goods_page_ready"):
            return worker._order_goods_for_all_stock_tabs(self.driver, "5000001")

    def test_partial_failure_retains_successes_and_retry_orders_only_failed_tab(self):
        self.driver.execute_script("nativeModes[3]='fail'")
        results = self.order_native()
        self.assertEqual([item["outcome"] for item in results], [
            "already_stock_ordered", "auto_order_succeeded", "native_stock_failed", "auto_order_succeeded",
        ])
        self.assertIn("Not enough stock", results[2]["message"])
        self.assertTrue(all(item["success"] for item in self.order_native()))
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4, 3])

    def test_lost_response_blocks_new_worker_and_changed_stock_before_unlocking(self):
        self.driver.execute_script("nativeModes[3]='reject'")
        results = self.order_native()
        self.assertEqual(results[2]["outcome"], "native_stock_uncertain")
        self.driver.execute_script("addSizeToDesign(3)")
        with mock.patch.object(worker, "_unlock_current_order_for_auto_ordering") as unlock, \
             mock.patch.object(worker, "_open_target_order_with_refresh") as open_order:
            retry = worker._run_order_with_driver(self.driver, "5000001")
        self.assertFalse(retry[0]["success"])
        unlock.assert_not_called()
        open_order.assert_not_called()
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4])

    def test_timeout_stops_without_automatic_retry(self):
        self.driver.execute_script("nativeModes[3]='timeout'")
        with mock.patch.object(native, "REQUEST_TIMEOUT_SECONDS", 0.1):
            results = self.order_native()
        self.assertEqual(results[2]["outcome"], "native_stock_uncertain")
        self.assertIsNotNone(native.recovery_block("5000001"))
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4])

    def test_response_without_persisted_stock_is_not_success_and_is_not_repeated(self):
        self.driver.execute_script("nativeModes[3]='not-saved'")
        results = self.order_native()
        self.assertEqual(results[2]["outcome"], "native_stock_not_verified")
        self.assertFalse(results[2]["success"])
        self.assertFalse(self.order_native()[2]["success"])
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4])

    def test_locked_hidden_disabled_manual_and_add_box_controls_are_excluded(self):
        self.driver.execute_script("""
          document.querySelector('#design-2 .order-goods').disabled=true;
          document.querySelector('#design-3 .order-goods').classList.add('ng-hide');
          const add=document.createElement('button'); add.setAttribute('ng-click','orderGoodsAddBox(provider);');
          add.textContent='Add Box'; document.getElementById('design-3').append(add);
        """)
        results = self.order_native()
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [4])
        self.assertEqual(results[1]["outcome"], "order_goods_locked")
        self.assertFalse(results[2]["success"])

    def test_durable_receipt_is_required_before_any_native_submission(self):
        with mock.patch.object(native, "_save", side_effect=OSError("disk full")):
            results = self.order_native()
        self.assertTrue(all(not item["success"] for item in results))
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [])

    def test_no_native_service_never_falls_back_to_serial_clicks(self):
        self.driver.execute_script("delete inventoryService.orderGoodsSupplynet")
        results = self.order_native()
        self.assertTrue(all(not item["success"] for item in results[1:]))
        self.assertTrue(all(item["outcome"] == "native_stock_not_sent" for item in results[1:]))
        self.assertIn("service is not ready", results[1]["message"])
        state = self.driver.execute_script("return stockFixtureState()")
        self.assertEqual(state["clicks"], [])
        self.assertEqual(state["legacyClicks"], 0)

    def test_dropped_worker_connection_preserves_durable_reservation(self):
        execute = self.driver.execute_script

        def disconnected(script, *args):
            if script == native._script() and args[0] == "status":
                raise worker.TimeoutException("Simulated disconnected worker")
            return execute(script, *args)

        with mock.patch.object(self.driver, "execute_script", side_effect=disconnected):
            results = self.order_native()
        self.assertEqual([item["outcome"] for item in results[1:]], ["native_stock_uncertain"] * 3)
        # A fresh worker profile has no session receipt. Disk receipts must
        # still stop it even if CRM has since changed the quantities.
        self.driver.execute_script("sessionStorage.removeItem('crm-order-all-native:5000001'); addSizeToDesign(3)")
        self.assertTrue(all(not item["success"] for item in self.order_native()))
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4])

    def test_stock_changed_after_preflight_is_not_submitted(self):
        execute = self.driver.execute_script

        def changed(script, *args):
            if script == native._script() and args[0] == "start":
                execute("addSizeToDesign(3)")
            return execute(script, *args)

        with mock.patch.object(self.driver, "execute_script", side_effect=changed):
            results = self.order_native()
        self.assertTrue(all(not item["success"] for item in results[1:]))
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [])
        self.assertIsNone(native.recovery_block("5000001"))

    def test_duplicate_design_ids_or_wrong_order_stop_before_submission(self):
        for change in ("resource.designs[2].id=resource.designs[1].id", "resource.id='5000002'"):
            with self.subTest(change=change):
                self.driver.refresh()
                self.driver.execute_script(change)
                results = self.order_native()
                self.assertTrue(all(not item["success"] for item in results))
                self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [])

    def test_removed_designs_and_manual_stock_are_excluded(self):
        self.driver.execute_script("""
          resource.designs[2].crudAction='d';
          document.querySelectorAll('#main-header-design-tabs button')[2].remove();
        """)
        results = self.order_native()
        self.assertTrue(all(item["success"] for item in results))
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 4])

    def test_missing_supplier_is_uncertain_and_cannot_be_retried_automatically(self):
        self.driver.execute_script("nativeModes[3]='unconfirmed'")
        results = self.order_native()
        self.assertEqual(results[2]["outcome"], "native_stock_uncertain")
        self.assertTrue(all(not item["success"] for item in self.order_native()))
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4])

    def test_waits_for_all_design_inventories_before_starting_requests(self):
        self.driver.execute_script("""
          const providers=inventoryScopes[2].providers;
          inventoryScopes[2].providers=[];
          setTimeout(()=>{inventoryScopes[2].providers=providers;},100);
        """)
        self.assertTrue(all(item["success"] for item in self.order_native()))
        state = self.driver.execute_script("return stockFixtureState()")
        self.assertEqual(state["events"][:3], ["start:2", "start:3", "start:4"])


if __name__ == "__main__":
    unittest.main()
