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
import ssactivewear_ordering as sxs


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
        patcher = mock.patch.object(sxs, "_require_site")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_assignment_model_remains_authoritative_under_dark_mode(self):
        self.driver.execute_script("document.body.classList.add('theme');document.querySelectorAll('.goods-ordered tr[ng-repeat]').forEach(r=>r.style.backgroundColor='green')")
        result = worker._read_rows(self.driver)
        self.assertEqual([x["assigned"] for x in result["records"]], [False, True])
        self.assertEqual(result["records"][1]["po"], "5000001")

    def test_archive_inline_red_yellow_fallback_and_unknown_state(self):
        self.driver.execute_script("delete window.angular")
        self.assertEqual([x["assigned"] for x in worker._read_rows(self.driver)["records"]], [False, True])
        self.driver.execute_script("document.querySelector('tr[ng-repeat]').style.backgroundColor='green'")
        self.assertIsNone(worker._read_rows(self.driver)["records"][0]["assigned"])

    def test_sxs_inventory_quantities_and_cart_match_the_crm_sizes(self):
        self.driver.execute_script("const count=document.createElement('span');count.id='qtyMiniCart';count.textContent='0';document.body.append(count);document.querySelector('#cart-link').setAttribute('aria-label','Cart: 99 items')")
        inventory = sxs.inventory(self.driver)
        self.assertEqual(inventory[0]["warehouse"], "Reading, PA")
        self.assertEqual(inventory[0]["stock"]["S"], ">50,000")
        line = {"product": {"product_id": "G500", "color": "Black"}, "selected": {"url": "https://www.ssactivewear.com/p/gildan/5000"}, "quantities": {"L": 1, "XL": 1, "2XL": 1}, "warehouse": "Reading, PA"}
        with mock.patch.object(sxs, "safe_get_with_partial_load") as navigate:
            sxs.fill_product(self.driver, [line])
        navigate.assert_called_once()
        rows = sxs.cart_rows(self.driver)
        self.assertEqual({r["size"]: r["quantity"] for r in rows}, {"L": 1, "XL": 1, "2XL": 1})
        sxs.validate_cart(rows, [line])

    def test_checkout_verifies_exact_po_address_ups_and_net60(self):
        result = sxs.review_checkout(self.driver, "5000001")
        self.assertEqual(result["po"], "5000001")
        self.assertEqual(result["payment"], "Net 60")
        self.assertEqual(self.driver.execute_script("return window.completeClicks"), 0)
        for script in ("document.querySelector('#shipping-address').textContent='2727 Commerce Way, Other City PA 19154'", "document.querySelector('select[id$=zFreightServiceCB]').selectedIndex=0", "document.querySelector('#M_M_zTermsCB').selectedIndex=0"):
            self.driver.get(self.fixture)
            self.driver.execute_script(script)
            with self.assertRaises(Exception):
                sxs.review_checkout(self.driver, "5000001")

    def test_grouped_warehouse_breakdown_is_read_and_closed_without_applying(self):
        self.driver.execute_script(r"""
document.querySelector('#M_M_zCart').innerHTML='<div class="cR" id="cartRow_777_-1"><div class="cDes"><a href="https://www.ssactivewear.com/p/gildan/5000/black-heavy_cotton"><b>5000</b></a></div><div class="cCol">Black</div><div class="cSiz">L</div><div class="cMet"><a role="button" aria-haspopup="true">S&amp;S Ship</a></div><div class="cLoc">Multiple warehouses</div><div class="cPie"><input value="2"></div></div>';
const popup=document.createElement('div');popup.id='modalPopupBody';popup.className='modal';popup.style.display='none';
popup.innerHTML='<div id="popupBody"><table><tr><td>Reading, PA</td><td><input type="number" value="1"></td></tr><tr><td>Middleboro, MA</td><td><input type="number" value="1"></td></tr></table></div><button class="popup-close">Cancel</button><button id="apply-breakdown">Apply</button>';
document.body.append(popup);window.applied=0;window.breakdownClosed=0;
document.querySelector('#cartRow_777_-1 .cMet a').onclick=()=>popup.style.display='block';
popup.querySelector('.popup-close').onclick=()=>{window.breakdownClosed++;popup.style.display='none'};
popup.querySelector('#apply-breakdown').onclick=()=>window.applied++;
""")
        common = {"product": {"product_id": "G500", "color": "Black"}, "selected": {"url": "https://www.ssactivewear.com/p/gildan/5000"}, "quantities": {"L": 1}}
        lines = [{**common, "warehouse": "Reading, PA"}, {**common, "warehouse": "Middleboro, MA"}]
        rows = sxs.verified_cart_rows(self.driver, lines)
        sxs.validate_cart(rows, lines)
        self.assertEqual(rows[0]["allocations"], {"Reading, PA": 1, "Middleboro, MA": 1})
        self.assertEqual(self.driver.execute_script("return [window.applied,window.breakdownClosed]"), [0, 1])

    def test_complete_and_final_confirmation_are_each_clicked_once(self):
        parse = sxs.confirmation_from_text
        def local_parse(text, po, url):
            self.assertTrue(url.startswith("file:"))
            return parse(text, po, "https://www.ssactivewear.com/checkout/confirmation")
        with mock.patch.object(sxs, "confirmation_from_text", side_effect=local_parse), mock.patch.object(sxs, "safe_take_screenshot", return_value="fixture.png"):
            receipt = sxs.submit(self.driver, "5000001")
        self.assertEqual(receipt["order_number"], "98765432")
        self.assertEqual(self.driver.execute_script("return [window.completeClicks,window.confirmClicks]"), [1, 1])


if __name__ == "__main__":
    unittest.main()
