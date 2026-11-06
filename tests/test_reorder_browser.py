"""DOM verification with sanitized offline markup from the supplied archives."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
import crm_reorder as worker


@unittest.skipUnless(os.environ.get("SANMAR_FIXTURE_CHROMEDRIVER"), "Set SANMAR_FIXTURE_CHROMEDRIVER for offline Reorder browser tests")
class ReorderBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = ROOT / "runtime" / "generated_profiles"
        root.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="reorder_fixture_", dir=root)
        assert Path(cls.temp.name).resolve().is_relative_to(root.resolve())
        options = webdriver.ChromeOptions()
        if os.name == "nt":
            options.add_argument("--no-sandbox")  # Isolated offline fixture profile only.
        options.add_argument("--disable-gpu")
        for flag in ("--headless=new", "--disable-background-networking", "--disable-sync", "--no-first-run", "--no-default-browser-check", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost"):
            options.add_argument(flag)
        options.add_argument(f"--user-data-dir={cls.temp.name}/chrome")
        if os.environ.get("SANMAR_FIXTURE_CHROME"):
            options.binary_location = os.environ["SANMAR_FIXTURE_CHROME"]
        cls.driver = webdriver.Chrome(service=Service(os.environ["SANMAR_FIXTURE_CHROMEDRIVER"], log_output=str(Path(cls.temp.name) / "driver.log")), options=options)
        cls.fixture = (ROOT / "tests" / "fixtures" / "reorder_stock.html").as_uri()

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit(); cls.temp.cleanup()

    def setUp(self):
        self.driver.get(self.fixture)

    def test_assignment_model_remains_authoritative_under_dark_mode(self):
        self.driver.execute_script("document.body.classList.add('theme');document.querySelectorAll('.goods-ordered tr[ng-repeat]').forEach(r=>r.style.backgroundColor='green')")
        result = worker._read_rows(self.driver)
        self.assertEqual([x["assigned"] for x in result["records"]], [False, True])
        self.assertEqual(result["records"][1]["po"], "5000001")

    def test_archive_inline_red_yellow_fallback_and_unknown_state(self):
        self.driver.execute_script("delete window.angular")
        self.assertEqual([x["assigned"] for x in worker._read_rows(self.driver)["records"]], [False, True])
        self.driver.execute_script("document.querySelector('tr[ng-repeat]').style.backgroundColor='green'")
        with self.assertRaisesRegex(RuntimeError, "could not be verified"):
            worker._read_rows(self.driver, timeout=0)

    def test_delayed_red_stock_waits_for_the_inventory_table_instead_of_skipping_the_order(self):
        self.driver.execute_script("""
const table=document.querySelector('table.goods-ordered');
const original=window.angular.element;
const rows=[...table.querySelectorAll('tbody tr')];
rows.forEach(row=>row.remove());table.style.display='none';
let inventory=[];
window.angular.element=node=>node===table ? {scope:()=>({inventoryOrders:inventory})} : original(node);
setTimeout(()=>{inventory=[{},{}];table.tBodies[0].append(...rows);table.style.display='';},1200);
""")
        with mock.patch.object(worker.bypass, '_open_target_order'), mock.patch.object(
            worker.bypass, '_require_crm_order_ready_once_with_refresh'
        ), mock.patch.object(worker.bypass, '_find_stock_tabs', return_value=[{'label':'H-Fixture 1 - QTY: 1 View Proofs'}]), mock.patch.object(
            worker.bypass, '_activate_stock_tab', return_value={}
        ), mock.patch.object(worker.bypass, '_extract_order_data', return_value={'products':[{'product_id':'CE10','color':'True Royal','quantities':{'M':1}}]}):
            _order,tabs=worker.scan_order(self.driver,'5000001')
        self.assertEqual(len(tabs),1)
        self.assertEqual(tabs[0]['vendor'],'sanmar')
        self.assertEqual(tabs[0]['red_records'][0]['po'],'H-Fixture-OLD')

    def test_stock_waits_for_all_model_rows_to_render_before_accepting_a_yellow_only_table(self):
        self.driver.execute_script("""
const table=document.querySelector('table.goods-ordered'), original=window.angular.element;
const red=table.querySelector('tbody tr');red.remove();
window.angular.element=node=>node===table ? {scope:()=>({inventoryOrders:[{},{}]})} : original(node);
setTimeout(()=>table.tBodies[0].prepend(red),900);
""")
        self.assertEqual([row['assigned'] for row in worker._read_rows(self.driver)['records']],[False,True])

    def test_a_table_that_never_loads_requires_review(self):
        self.driver.execute_script("document.querySelector('table.goods-ordered').remove()")
        with self.assertRaisesRegex(RuntimeError,'Reorder requires review'):
            worker._read_rows(self.driver,timeout=0)

    def test_angular_inventory_must_be_initialized_before_rows_are_accepted(self):
        self.driver.execute_script("""
const table=document.querySelector('table.goods-ordered'), original=window.angular.element;
window.angular.element=node=>node===table ? {scope:()=>({inventoryOrders:undefined})} : original(node);
""")
        with self.assertRaisesRegex(RuntimeError,'Reorder requires review'):
            worker._read_rows(self.driver,timeout=0)

    def test_hidden_designs_cannot_supply_the_visible_designs_stock_rows(self):
        self.driver.execute_script("""
const table=document.querySelector('table.goods-ordered'), red=table.querySelector('tbody tr');
const pane=document.createElement('div');pane.className='tab-pane';pane.style.display='none';
pane.append(table.cloneNode(true));document.body.append(pane);red.remove();
""")
        self.assertEqual([row['assigned'] for row in worker._read_rows(self.driver)['records']],[True])


    def test_production_note_waits_for_append_field_and_retry_keeps_history_unique(self):
        fixture = (ROOT / 'tests/fixtures/crm_append_production_note.html').as_uri()
        self.driver.get(fixture)
        self.driver.execute_script('localStorage.clear()')
        self.driver.refresh()
        note = 'Reordered stock - PO 5000001 - Sanmar fixture-reference'
        with mock.patch.object(worker.bypass, '_wait_for_order_goods_page_ready'):
            self.assertEqual(worker.bypass._save_and_verify_crm_production_note(self.driver, '5000001', note), 'recorded')
            self.assertEqual(worker.bypass._save_and_verify_crm_production_note(self.driver, '5000001', note), 'already_present')
        history = self.driver.execute_script("return localStorage.getItem('append_production_notes')")
        self.assertEqual(history, 'Existing production note\n' + note)
        self.assertEqual(self.driver.execute_script("return document.querySelector('#sales').value"), 'Existing sales note')

    def test_production_note_verification_rejects_an_unsaved_append_draft(self):
        fixture = (ROOT / 'tests/fixtures/crm_append_production_note.html').as_uri()
        self.driver.get(fixture)
        self.driver.execute_script("const input=document.createElement('textarea');input.value='Unsaved draft only';document.querySelector('#order-notes-production-notes').append(input)")
        with self.assertRaises(worker.bypass._CrmProductionNotePersistenceError):
            worker.bypass._verify_crm_production_note(self.driver, '5000001', 'Unsaved draft only', timeout=0)


if __name__ == "__main__":
    unittest.main()
