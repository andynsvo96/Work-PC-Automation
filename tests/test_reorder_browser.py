"""DOM verification with sanitized offline markup from the supplied archives."""

import os
import sys
import tempfile
import time
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

    def test_delayed_cookie_banner_is_accepted_before_clicking_the_page(self):
        self.driver.execute_script(r"""
localStorage.removeItem('reorder_fixture_cookie_consent');
window.cookieAccepts=0;window.nextClicks=0;window.unrelatedClicks=0;
const next=document.createElement('button');next.id='cookie-guarded-next';next.textContent='Continue';next.disabled=true;next.style='position:fixed;top:40px;left:20px';next.onclick=()=>window.nextClicks++;document.body.append(next);
const unrelated=document.createElement('button');unrelated.textContent='Accept All Cookies';unrelated.onclick=()=>window.unrelatedClicks++;document.body.append(unrelated);
const banner=document.createElement('div');banner.id='onetrust-banner-sdk';banner.style='position:fixed;inset:0;z-index:99999;background:white;display:none';banner.innerHTML='<button id="onetrust-accept-btn-handler">Accept All Cookies</button>';document.body.append(banner);
banner.querySelector('button').onclick=()=>{window.cookieAccepts++;localStorage.setItem('reorder_fixture_cookie_consent','accepted');banner.style.display='none';next.disabled=false};
setTimeout(()=>banner.style.display='block',150);
""")
        sxs._click(self.driver, '#cookie-guarded-next')
        self.assertEqual(self.driver.execute_script('return [window.cookieAccepts,window.nextClicks,window.unrelatedClicks]'), [1, 1, 0])
        self.assertFalse(sxs.accept_cookie_banner(self.driver))
        self.driver.refresh()
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('reorder_fixture_cookie_consent')"), 'accepted')
        self.assertFalse(sxs.accept_cookie_banner(self.driver))

    def test_sxs_login_uses_account_fields_and_recovers_initial_focus_redirect(self):
        self.driver.get((ROOT / 'tests/fixtures/ssactivewear_login.html').as_uri())
        credential = mock.Mock(username='account@example.test', secret='fixture-password')
        with mock.patch.object(sxs, 'safe_get_with_partial_load'), mock.patch.object(
            sxs, 'read_credential', return_value=credential
        ) as read:
            sxs.ensure_login(self.driver)
        read.assert_called_once_with(sxs.SSACTIVEWEAR_CREDENTIAL_TARGET)
        self.assertEqual(self.driver.execute_script("return document.querySelector('#newsletter').value"), '')
        self.assertEqual(self.driver.execute_script("return document.querySelector('#M_M_zEmailTB').value"), 'account@example.test')
        self.assertEqual(self.driver.execute_script('return window.loginClicks'), 1)
        self.assertEqual(self.driver.execute_script('return window.focusRedirects'), 2)
        self.assertTrue(self.driver.execute_script("return !!document.querySelector('#M_M_zCart')"))

    def _install_sxs_page_fixture(self):
        # Only this isolated file fixture bypasses the production HTTPS check.
        # Controller/transport origin boundaries have their own regression tests.
        source = (ROOT / 'crm-order-dark-mode-extension/sxs-page.js').read_text(encoding='utf-8')
        source = source.replace("location.origin !== 'https://www.ssactivewear.com'", "location.protocol !== 'file:'")
        source = source.replace('parsed.origin !== location.origin', "parsed.origin !== 'https://www.ssactivewear.com'")
        source = source.replace('url.origin === location.origin', "url.origin === 'https://www.ssactivewear.com'")
        self.driver.execute_script("""
window.chrome=window.chrome||{};
window.chrome.runtime={id:'fixture-extension',onMessage:{addListener:fn=>window.sxsFixtureMessage=fn}};
const signedIn=document.createElement('a');signedIn.href='https://www.ssactivewear.com/myaccount/signout';signedIn.textContent='Sign Out';document.body.append(signedIn);
""")
        self.driver.execute_script(source)

    def _sxs_page_command(self, operation, payload=None, order_id='5000001', deadline=None):
        message = {'type': 'sxs:page-command', 'commandId': 'a' * 32, 'operation': operation,
                   'payload': payload or {}, 'orderId': order_id, 'deadline': deadline or time.time() + 40}
        return self.driver.execute_async_script("const done=arguments[arguments.length-1];window.sxsFixtureMessage(arguments[0],{id:'fixture-extension'},done);", message)

    def test_extension_page_stock_and_checkout_flow_with_exact_confirmation(self):
        self._install_sxs_page_fixture()
        inventory = self._sxs_page_command('inventory')
        self.assertEqual(inventory['result']['inventory'][0]['warehouse'], 'Reading, PA')
        quantities = {'L': 1, 'XL': 1, '2XL': 1}
        lines = [{'product': {'product_id': 'G500', 'color': 'Black'}, 'selected': {'url': 'https://www.ssactivewear.com/p/gildan/5000'}, 'quantities': quantities, 'warehouse': 'Reading, PA'}]
        self.assertTrue(self._sxs_page_command('fill', {'allocations': [{'warehouse': 'Reading, PA', 'quantities': quantities}]})['success'])
        self.assertTrue(self._sxs_page_command('add', {'quantity': 3})['success'])
        rows = self._sxs_page_command('verified_cart', {'lines': lines})['result']['rows']
        sxs.validate_cart(rows, lines)
        for operation in ('checkout', 'review', 'begin_submit', 'confirm_submit'):
            result = self._sxs_page_command(operation, {'po': '5000001'})
            self.assertTrue(result['success'], result)
        confirmation = self._sxs_page_command('confirmation')['result']['text']
        self.assertEqual(sxs.confirmation_from_text(confirmation, '5000001', 'https://www.ssactivewear.com/checkout/confirmation')['order_number'], '98765432')
        self.assertEqual(self.driver.execute_script('return [window.completeClicks,window.confirmClicks]'), [1, 1])

    def test_extension_search_reads_product_cards_when_links_have_no_layout_box(self):
        self.driver.execute_script("""
const products=document.createElement('div');products.id='M_M_zProducts';
products.innerHTML=`
<div class="gProduct"><a style="display:contents" href="https://www.ssactivewear.com/p/gildan/5000"><div class="gName">5000</div><ins class="gBrand">Gildan</ins><ins class="gTitle">Unisex Heavy Cotton T-Shirt</ins></a><a href="https://www.ssactivewear.com/p/gildan/5000">+72 more colors</a></div>
<div class="gProduct"><a style="display:contents" href="https://www.ssactivewear.com/p/bayside/5000"><div class="gName">5000</div><ins class="gBrand">Bayside</ins><ins class="gTitle">Unisex USA-Made Fine Jersey T-Shirt</ins></a></div>
<div class="gProduct"><a style="display:contents" href="https://www.ssactivewear.com/p/gildan/5000b"><div class="gName">5000B</div><ins class="gBrand">Gildan</ins><ins class="gTitle">Youth Heavy Cotton T-Shirt</ins></a></div>
<div class="gProduct"><div class="gName">5000</div><a href="https://www.ssactivewear.com/p/gildan/5000">5000</a><a href="https://www.ssactivewear.com/p/bayside/5000">5000</a><ins class="gBrand">Gildan</ins><ins class="gTitle">Heavy Cotton T-Shirt</ins></div>`;
document.body.append(products);
""")
        self._install_sxs_page_fixture()
        result = self._sxs_page_command('candidates')
        self.assertTrue(result['success'], result)
        candidates = result['result']['candidates']
        self.assertEqual(len(candidates), 3)
        selected = sxs.select_product({'product_id': 'G500', 'product_name': 'Gildan Heavy Cotton T-Shirt'}, candidates)
        self.assertEqual(selected['url'], 'https://www.ssactivewear.com/p/gildan/5000')
        self.assertNotIn('more colors', selected['title'])

    def test_extension_color_handler_waits_for_matching_inventory_and_delivery_dates(self):
        self.driver.execute_script("""
document.querySelector('#zcolorname').innerHTML='83 Colors - Selected Color: <strong>White - PMS: 11-0601 TCX</strong>';
const swatches=document.querySelector('#colorSwatch');swatches.innerHTML='';
for(const [id,color] of [['16813','White'],['16787','Black']]) {
 const a=document.createElement('a');a.id='cSwatch'+id;a.textContent=color;
 a.href=`javascript: iColor('${id}','false','false','True','');`;
 a.classList.toggle('active',color==='White');swatches.append(a);
}
document.querySelectorAll('.gR input').forEach(i=>i.setAttribute('csid','16813'));
document.querySelectorAll('.shippingRowLabel').forEach(e=>e.textContent='');
window.colorCalls=0;window.iColor=id=>{window.colorCalls++;setTimeout(()=>{
 document.querySelector('#zcolorname strong').textContent='Black - PMS: 426';
 document.querySelectorAll('#colorSwatch a').forEach(a=>a.classList.toggle('active',a.id==='cSwatch'+id));
 document.querySelectorAll('.gR input').forEach(i=>i.setAttribute('csid',id));
 setTimeout(()=>document.querySelectorAll('.shippingRowLabel').forEach(e=>e.textContent='Est. Delivery: 11/04/2026'),150);
},100)};
""")
        self._install_sxs_page_fixture()
        ready = self._sxs_page_command('color_ready', {'color': 'Black'})
        self.assertTrue(ready['success'], ready)
        self.assertEqual(ready['result']['anchor_id'], 'cSwatch16787')
        source = (ROOT / 'crm-order-dark-mode-extension/ssactivewear-worker.mjs').read_text(encoding='utf-8')
        source = 'function selectSxsColorInMain' + source.split('export function selectSxsColorInMain', 1)[1].split('\nexport function createSxsWorker', 1)[0]
        # Only the isolated file fixture substitutes the production URL boundary.
        source = source.replace("location.origin !== 'https://www.ssactivewear.com'", "location.protocol !== 'file:'")
        source = source.replace(r'!/^\/p\/[^/]+\/[^/]+\/?$/.test(location.pathname)', 'false')
        args = {'color': 'Black', 'anchorId': 'cSwatch16787', 'orderId': '5000001', 'deadline': time.time() + 40}
        self.assertTrue(self.driver.execute_script('return (' + source + ')(arguments[0]);', args)['success'])
        self.assertTrue(self._sxs_page_command('color', {'color': 'Black'})['success'])
        inventory = self._sxs_page_command('inventory')
        self.assertTrue(inventory['success'], inventory)
        self.assertEqual(inventory['result']['inventory'][0]['eta_text'], 'Est. Delivery: 11/04/2026')
        self.assertEqual(self.driver.execute_script('return window.colorCalls'), 1)
        self.assertFalse(self.driver.execute_script("return [...document.querySelectorAll('.gR input')].some(i=>i.value)"))
        self.driver.execute_script("document.querySelector('#cSwatch16787').href='javascript:alert(1)'")
        with self.assertRaisesRegex(Exception, 'handler is unavailable or changed'):
            self.driver.execute_script('return (' + source + ')(arguments[0]);', args)
        self.assertEqual(self.driver.execute_script('return window.colorCalls'), 1)

    def test_extension_inventory_accepts_the_live_warehouse_shipping_text_layout(self):
        self.driver.execute_script("""
document.querySelectorAll('.shipby').forEach(e=>e.innerHTML='<span>Ships next business day</span><span>Est. Delivery: Wed 11/4</span>');
""")
        self._install_sxs_page_fixture()
        result = self._sxs_page_command('inventory')
        self.assertTrue(result['success'], result)
        self.assertEqual(len(result['result']['inventory']), 2)
        from datetime import date
        self.assertEqual(sxs.parse_eta(result['result']['inventory'][0]['eta_text'], date(2026, 11, 2)), date(2026, 11, 4))

    def test_extension_stock_sizes_use_the_own_grid_headers_when_cell_labels_are_absent(self):
        self.driver.execute_script("""
const grid=document.querySelector('#inventory');grid.classList.add('oGrid');
const header=document.createElement('div');header.className='gH';
header.innerHTML='<span class="color">Color</span>'+['S','M','L','XL','2XL','3XL','4XL','5XL'].map(s=>'<span>'+s+'</span>').join('');grid.prepend(header);
document.querySelectorAll('.gR .i .sr-only').forEach(e=>e.remove());
const other=document.createElement('div');other.className='oGrid';other.innerHTML='<div class="gH"><span class="color">Color</span><span>Unrelated</span></div>';document.body.prepend(other);
""")
        self._install_sxs_page_fixture()
        result = self._sxs_page_command('inventory')
        self.assertTrue(result['success'], result)
        self.assertEqual(result['result']['inventory'][0]['stock']['L'], '120')
        self.assertNotIn('', result['result']['inventory'][0]['stock'])
        fill = self._sxs_page_command('fill', {'allocations': [{'warehouse': 'Reading, PA', 'quantities': {'L': 1}}]})
        self.assertTrue(fill['success'], fill)
        self.assertEqual(self.driver.execute_script("return document.querySelector('#q_12_L').value"), '1')
        self.driver.execute_script("document.querySelector('#inventory .gH').lastElementChild.remove()")
        denied = self._sxs_page_command('fill', {'allocations': [{'warehouse': 'Reading, PA', 'quantities': {'L': 2}}]})
        self.assertFalse(denied['success'])
        self.assertIn('do not match', denied['message'])
        self.assertEqual(self.driver.execute_script("return document.querySelector('#q_12_L').value"), '1')

    def test_extension_refuses_unreadable_stock_sizes_instead_of_returning_an_empty_size_key(self):
        self.driver.execute_script("document.querySelectorAll('.gR .i .sr-only').forEach(e=>e.remove())")
        self._install_sxs_page_fixture()
        result = self._sxs_page_command('inventory')
        self.assertFalse(result['success'])
        self.assertIn('size labels are missing', result['message'])

    def test_extension_page_refuses_wrong_po_and_expired_or_unowned_mutations(self):
        self._install_sxs_page_fixture()
        denied = self._sxs_page_command('fill', {'allocations': []}, order_id='')
        self.assertFalse(denied['success'])
        self.assertIn('active Reorder', denied['message'])
        self.assertFalse(self._sxs_page_command('begin_submit', {'po': '5000002'})['success'])
        self.assertFalse(self._sxs_page_command('begin_submit', {'po': '5000001'}, deadline=1)['success'])
        self.assertTrue(self._sxs_page_command('review', {'po': '5000001'})['success'])
        self.assertTrue(self._sxs_page_command('begin_submit', {'po': '5000001'})['success'])
        self.driver.execute_script("document.querySelector('#M_M_zPONumberTB').value='5000002'")
        self.assertFalse(self._sxs_page_command('confirm_submit', {'po': '5000001'})['success'])
        self.assertEqual(self.driver.execute_script('return window.confirmClicks'), 0)

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
