"""Extra Print Areas save/retry checks against the shared offline CRM fixture."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

from workers import crm_reverse_prints as reverse
from workers import crm_sleeve_prints as extra


ROOT = Path(__file__).resolve().parents[1]
SELECTION = [{"tab_number": 1, "quantity": 9, "extra_emb": "embroidery"}]
NOTE = "Additional embroidery area\n$9.00 each\nemailed txted"


class ExtraPrintAreasBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                driver_path = str(max(candidates, key=lambda path: tuple(
                    int(number) for number in next(part for part in path.parts if part.count(".") == 3).split(".")
                )))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver available for offline Extra Print Areas tests.")
        cls.artifacts = ROOT / "runtime/debug/extra_print_areas_tests"
        cls.artifacts.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=cls.artifacts)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for argument in ("--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox"):
            options.add_argument(argument)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)
        cls.fixture_url = (ROOT / "tests/fixtures/copyright_cancel_crm.html").as_uri()

    def install_controls(self):
        self.driver.execute_script("""
          scope.STATICS = {
            printAreaTemplates: ['Front-Left Chest', 'Front-Right Chest'].map(description => ({description})),
            printMethods: [{description: 'Embroidery'}]
          };
          scope.addPrintArea = design => design.printAreas.push({});
          scope.updateAreaTemplate = (area, template) => area.description = template.description;
          scope.updateAreaMethod = (area, method) => area.methodDescription = method.description;
          scope.watchItemChanges = () => localStorage.setItem('extra-price-writes',
            String(Number(localStorage.getItem('extra-price-writes') || 0) + 1));
        """)

    def navigate(self, _driver, _url, _label):
        self.driver.get(self.fixture_url)
        self.install_controls()

    def setUp(self):
        self.driver.get(self.fixture_url)
        self.driver.execute_script("""
          resetFixture();
          localStorage.removeItem('extra-price-writes');
          localStorage.setItem('copyright-reset-note-on-edit', 'true');
          resource.designs = [{id: 101, designId: 101, name: 'Offline embroidery', quantity: 9, subtotal: '450.45',
            printAreas: [{description: 'Front', methodDescription: 'Embroidery'}],
            designItems: [{splitIntoSizes: 0, pricePerPiece: '50.05'}]}];
          resource.salesNotes = 'Earlier note';
          persist(); render();
        """)
        self.install_controls()
        self.receipts = tempfile.TemporaryDirectory(prefix="receipts-", dir=self.artifacts)
        self.addCleanup(self.receipts.cleanup)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in ("_login_to_crm_if_needed", "_switch_to_crm_app_frame", "_wait_for_order_scope",
                     "safe_driver_quit", "safe_take_screenshot"):
            stack.enter_context(patch.object(extra.shared, name))
        stack.enter_context(patch.object(extra.shared, "safe_get_with_partial_load", side_effect=self.navigate))
        stack.enter_context(patch.object(extra.shared, "_open_driver", return_value=self.driver))
        stack.enter_context(patch.object(extra.shared, "_wait_for_crm_contact_info",
                                         return_value={"email": "customer@example.test"}))
        stack.enter_context(patch.object(reverse, "STATE_DIR", self.receipts.name))
        self.apply = stack.enter_context(patch.object(extra, "_apply_crm_sleeve_changes", wraps=extra._apply_crm_sleeve_changes))
        self.invoice = stack.enter_context(patch.object(extra, "_capture_view_invoice_link", return_value="https://example.test/invoice"))
        self.email = stack.enter_context(patch.object(extra, "_prepare_and_send_salesforce_email", return_value={"sent": True}))

    def process(self):
        return extra.process_sleeve_prints_order("5451301", SELECTION, embroidery_price="9.00")

    def receipt(self):
        paths = list(Path(self.receipts.name).rglob("*.crm.json"))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def test_editor_initialization_does_not_lose_note_or_price_and_retry_sends_once(self):
        self.assertTrue(self.process()["success"])
        path = self.receipt()
        saved = path.read_bytes()
        self.assertTrue(self.process()["salesforce"]["skipped"])
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.apply.call_count, 1)
        self.email.assert_called_once()
        self.assertEqual(self.driver.execute_script("return resource.designs[0].designItems[0].pricePerPiece"), "59.05")
        self.assertEqual(extra._read_crm_sleeve_state(self.driver)["sales_notes"].count(NOTE), 1)
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('extra-price-writes')"), "1")

    def test_missing_note_gets_one_save_retry_then_receipt_recovers_without_repricing(self):
        self.driver.execute_script("localStorage.setItem('copyright-note-save-failures', '2');")
        with self.assertRaisesRegex(extra.SleevePrintsError, "one note-only save retry"):
            self.process()
        path = self.receipt()
        saved = path.read_bytes()
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('copyright-note-save-attempts')"), "2")
        self.invoice.assert_not_called()
        self.email.assert_not_called()
        self.assertTrue(self.process()["crm_order_update"]["verification"]["note_repaired"])
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.apply.call_count, 1)
        self.email.assert_called_once()
        self.assertTrue(self.process()["salesforce"]["skipped"])
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('extra-price-writes')"), "1")
        self.assertEqual(extra._read_crm_sleeve_state(self.driver)["sales_notes"].count(NOTE), 1)

    def test_unsaved_or_partially_saved_receipt_stops_before_note_or_email(self):
        plan = extra._build_live_plan(extra.normalize_request(SELECTION, embroidery_price="9.00"),
                                      extra._read_crm_sleeve_state(self.driver))
        mutation = extra._apply_crm_sleeve_changes(self.driver, plan, NOTE)
        path = reverse.email_receipt_path("5451301", "customer@example.test", plan, {"jobs": []}).with_suffix(".crm.json")
        extra._record_emb_mutation(path, mutation)
        saved = path.read_bytes()
        self.apply.reset_mock()
        # The draft was never saved: navigation restores the original CRM state.
        with self.assertRaisesRegex(extra.SleevePrintsError, "Extra Print Areas were not confirmed"):
            self.process()
        self.assertEqual(path.read_bytes(), saved)
        self.apply.assert_not_called()
        self.assertIsNone(self.driver.execute_script("return localStorage.getItem('copyright-note-save-attempts')"))
        # A partial save with the requested areas but original prices also stops.
        self.driver.execute_script("""
          resource.designs[0].printAreas = ['Front-Left Chest', 'Front-Right Chest'].map(description =>
            ({description, methodDescription: 'Embroidery'}));
          persist();
        """)
        with self.assertRaisesRegex(extra.SleevePrintsError, "prices were not confirmed"):
            self.process()
        self.assertEqual(path.read_bytes(), saved)
        self.invoice.assert_not_called()
        self.email.assert_not_called()

    def test_incomplete_receipt_stops_for_review(self):
        self.process()
        path = self.receipt()
        path.write_text(json.dumps({"price_updates": []}), encoding="utf-8")
        self.email.reset_mock()
        with self.assertRaisesRegex(extra.SleevePrintsError, "no verifiable area or price targets"):
            self.process()
        self.email.assert_not_called()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"price_updates": []})


if __name__ == "__main__":
    unittest.main()
