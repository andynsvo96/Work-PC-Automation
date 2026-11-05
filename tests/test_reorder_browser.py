"""DOM verification with sanitized offline markup from the supplied archives."""

import os
import json
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
        self.assertEqual(tabs[0]['vendor'],'ssactivewear')
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

    def _install_sxs_page_fixture(self, path=None):
        # Only this isolated file fixture bypasses the production HTTPS check.
        # Controller/transport origin boundaries have their own regression tests.
        source = (ROOT / 'crm-order-dark-mode-extension/sxs-page.js').read_text(encoding='utf-8')
        source = source.replace("location.origin !== 'https://www.ssactivewear.com'", "location.protocol !== 'file:'")
        source = source.replace('parsed.origin !== location.origin', "parsed.origin !== 'https://www.ssactivewear.com'")
        source = source.replace('url.origin === location.origin', "url.origin === 'https://www.ssactivewear.com'")
        if path is not None:
            source = source.replace('location.pathname', json.dumps(path))
        self.driver.execute_script("""
window.chrome=window.chrome||{};
window.chrome.runtime={id:'fixture-extension',getManifest:()=>({version:'1.7.8'}),onMessage:{addListener:fn=>window.sxsFixtureMessage=fn}};
const signedIn=document.createElement('a');signedIn.href='https://www.ssactivewear.com/myaccount/signout';signedIn.textContent='Sign Out';document.body.append(signedIn);
""")
        self.driver.execute_script(source)

    def _sxs_page_command(self, operation, payload=None, order_id='5000001', deadline=None):
        message = {'type': 'sxs:page-command', 'commandId': 'a' * 32, 'operation': operation,
                   'payload': payload or {}, 'orderId': order_id, 'deadline': deadline or time.time() + 40}
        return self.driver.execute_async_script("const done=arguments[arguments.length-1];window.sxsFixtureMessage(arguments[0],{id:'fixture-extension'},done);", message)

    def _sxs_main_stock_control(self, operation, **payload):
        source = (ROOT / 'crm-order-dark-mode-extension/ssactivewear-worker.mjs').read_text(encoding='utf-8')
        source = 'function clickSxsStockControlInMain' + source.split('export function clickSxsStockControlInMain', 1)[1].split('\nexport function clickSxsCheckoutControlInMain', 1)[0]
        # File fixtures substitute only the production HTTPS and path guards.
        source = source.replace("location.origin !== 'https://www.ssactivewear.com'", "location.protocol !== 'file:'")
        source = source.replace(r'!/^\/p\/[^/]+\/[^/]+\/?$/.test(location.pathname)', 'false')
        source = source.replace("location.pathname !== '/checkout/cart'", 'false')
        args = {**payload, 'operation': operation, 'orderId': '5000001', 'deadline': time.time() + 40}
        return self.driver.execute_script('return (' + source + ')(arguments[0]);', args)

    def _sxs_main_checkout_control(self, operation, po='5000001', deadline=None):
        source = (ROOT / 'crm-order-dark-mode-extension/ssactivewear-worker.mjs').read_text(encoding='utf-8')
        source = 'function clickSxsCheckoutControlInMain' + source.split('export function clickSxsCheckoutControlInMain', 1)[1].split('\nexport function createSxsWorker', 1)[0]
        source = source.replace("location.origin !== 'https://www.ssactivewear.com'", "location.protocol !== 'file:'")
        source = source.replace(r'!/^\/checkout\/?$/.test(location.pathname)', 'false')
        args = {'operation': operation, 'orderId': '5000001', 'po': po, 'deadline': deadline or time.time() + 40}
        return self.driver.execute_script('return (' + source + ')(arguments[0]);', args)

    def _sxs_checkout_command(self, operation, payload, deadline=None):
        ready = self._sxs_page_command(operation + '_ready', payload, deadline=deadline)
        if not ready['success']:
            return ready
        self.assertTrue(self._sxs_main_checkout_control(operation, po=payload['po'], deadline=deadline)['click_issued'])
        return self._sxs_page_command(operation, payload, deadline=deadline)

    def test_extension_page_stock_and_checkout_flow_with_exact_confirmation(self):
        self._install_sxs_page_fixture()
        inventory = self._sxs_page_command('inventory')
        self.assertEqual(inventory['result']['inventory'][0]['warehouse'], 'Reading, PA')
        quantities = {'L': 1, 'XL': 1, '2XL': 1}
        lines = [{'product': {'product_id': 'G500', 'color': 'Black'}, 'selected': {'url': 'https://www.ssactivewear.com/p/gildan/5000'}, 'quantities': quantities, 'warehouse': 'Reading, PA'}]
        self.assertTrue(self._sxs_page_command('fill', {'allocations': [{'warehouse': 'Reading, PA', 'quantities': quantities}]})['success'])
        ready = self._sxs_page_command('add_ready', {'quantity': 3})
        self.assertTrue(ready['success'], ready)
        before = ready['result']['before']
        self.assertTrue(self._sxs_main_stock_control('add', quantity=3, before=before)['click_issued'])
        self.assertTrue(self._sxs_page_command('add', {'quantity': 3, 'before': before})['success'])
        rows = self._sxs_page_command('verified_cart', {'lines': lines})['result']['rows']
        sxs.validate_cart(rows, lines)
        for operation in ('checkout', 'review', 'begin_submit', 'confirm_submit'):
            command = self._sxs_checkout_command if operation in ('begin_submit', 'confirm_submit') else self._sxs_page_command
            result = command(operation, {'po': '5000001'})
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
        source = 'function selectSxsColorInMain' + source.split('export function selectSxsColorInMain', 1)[1].split('\nexport function clickSxsStockControlInMain', 1)[0]
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

    def test_extension_stock_controls_add_once_and_remove_only_the_exact_unchanged_cart(self):
        self._install_sxs_page_fixture()
        fill = self._sxs_page_command('fill', {'allocations': [{'warehouse': 'Reading, PA', 'quantities': {'L': 1}}]})
        self.assertTrue(fill['success'], fill)
        ready = self._sxs_page_command('add_ready', {'quantity': 1})
        self.assertTrue(ready['success'], ready)
        before = ready['result']['before']
        with self.assertRaisesRegex(Exception, 'quantities changed'):
            self._sxs_main_stock_control('add', quantity=2, before=before)
        self.assertEqual(self._sxs_page_command('cart')['result']['rows'], [])
        self.assertTrue(self._sxs_main_stock_control('add', quantity=1, before=before)['click_issued'])
        self.assertTrue(self._sxs_page_command('add', {'quantity': 1, 'before': before})['success'])
        # Flask sorts JSON object keys; stock identity must ignore key order.
        rows = [{key: row[key] for key in sorted(row)} for row in self._sxs_page_command('cart')['result']['rows']]
        self.assertEqual(len(rows), 1)
        self.assertTrue(self._sxs_page_command('remove_ready', {'rows': rows})['success'])
        self.driver.execute_script("document.querySelector('#M_M_zCart .cPie input').value='2'")
        with self.assertRaisesRegex(Exception, 'cart changed'):
            self._sxs_main_stock_control('remove', rows=rows)
        self.assertEqual(self.driver.execute_script("return Number(document.querySelector('#M_M_zCart .cPie input').value)"), 2)
        self.driver.execute_script("document.querySelector('#M_M_zCart .cPie input').value='1'")
        self.driver.execute_script("document.querySelector('#M_M_zCart').insertAdjacentHTML('afterbegin','<div class=\"cR\"><div class=\"cDes\">Earlier stock was removed</div></div>')")
        self.assertTrue(self._sxs_main_stock_control('remove', rows=rows)['click_issued'])
        self.assertTrue(self._sxs_page_command('remove', {'rows': rows})['success'])
        self.assertEqual(self._sxs_page_command('cart')['result']['rows'], [])

    def test_extension_cart_waits_for_rows_after_the_count_changes(self):
        self._install_sxs_page_fixture()
        self._sxs_page_command('fill', {'allocations': [{'warehouse': 'Reading, PA', 'quantities': {'L': 1}}]})
        self._sxs_main_stock_control('add', quantity=1, before=0)
        self.driver.execute_script("""
const cart=document.querySelector('#M_M_zCart'), html=cart.innerHTML;
cart.innerHTML='';document.querySelector('#cart-link').setAttribute('aria-label','Cart: 0 items');
setTimeout(()=>document.querySelector('#cart-link').setAttribute('aria-label','Cart: 1 items'),100);
setTimeout(()=>{cart.innerHTML=html},400);
""")
        result = self._sxs_page_command('cart', {'expected_quantity': 1})
        self.assertTrue(result['success'], result)
        self.assertEqual([r['quantity'] for r in result['result']['rows']], [1])

    def _sxs_live_remove_fixture(self):
        self._install_sxs_page_fixture()
        self._sxs_page_command('fill', {'allocations': [{'warehouse': 'Reading, PA', 'quantities': {'L': 1, 'XL': 1}}]})
        self._sxs_main_stock_control('add', quantity=2, before=0)
        self.driver.execute_script("""
window.removeCalls=[];
window.removeCartItem=(undo,id,warehouse)=>{
 window.removeCalls.push([id,warehouse]);document.getElementById(`cartRow_${id}_${warehouse}`).remove();window.updateFixtureCartCount();
};
document.querySelectorAll('#M_M_zCart .cR').forEach((row,index)=>{
 const id=423089+index;row.id=`cartRow_${id}_-1`;
 const link=row.querySelector('a[aria-label="remove item"]');link.removeAttribute('aria-label');link.removeAttribute('role');
 link.innerHTML='<svg width="16" height="16"><path d="M0 0L16 16"/></svg>';
 link.setAttribute('href',`javascript: removeCartItem(function(){updateCartDataOnChange(1, '/p/gildan/5000/black-heavy_cotton', 4505158,1,12,${id},false,false,false,35222,53856,0,-1,false,0)},${id},-1);`);
});
document.querySelector('#M_M_zCart').insertAdjacentHTML('afterbegin','<div class="cR">Previous stock was removed</div>');
""")
        return self._sxs_page_command('cart')['result']['rows']

    def test_extension_removes_the_live_unlabelled_cart_control_and_preserves_other_stock(self):
        rows = self._sxs_live_remove_fixture()
        self.assertTrue(self._sxs_page_command('remove_ready', {'rows': rows})['success'])
        self.assertTrue(self._sxs_main_stock_control('remove', rows=rows)['click_issued'])
        result = self._sxs_page_command('remove', {'rows': rows})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['result']['rows'], rows[1:])
        self.assertEqual(self.driver.execute_script('return window.removeCalls'), [[423089, -1]])

    def test_extension_refuses_live_removal_controls_for_other_rows_or_unknown_handlers(self):
        for href in (
            'javascript: removeCartItem(function(){},423090,-1);',
            'javascript: unknownCartAction(423089,-1);',
        ):
            with self.subTest(href=href):
                self.driver.get(self.fixture)
                rows = self._sxs_live_remove_fixture()
                self.driver.execute_script("const a=document.querySelector('#cartRow_423089_-1 .cPie a');a.href=arguments[0];a.setAttribute('aria-label','remove item')", href)
                self.assertFalse(self._sxs_page_command('remove_ready', {'rows': rows})['success'])
                with self.assertRaisesRegex(Exception, 'missing or ambiguous'):
                    self._sxs_main_stock_control('remove', rows=rows)
                self.assertEqual(self.driver.execute_script('return window.removeCalls'), [])
                self.assertEqual(self._sxs_page_command('cart')['result']['rows'], rows)

    def test_extension_refuses_competing_live_cart_removal_controls(self):
        rows = self._sxs_live_remove_fixture()
        self.driver.execute_script("const a=document.querySelector('#cartRow_423089_-1 .cPie a');a.after(a.cloneNode(true))")
        self.assertFalse(self._sxs_page_command('remove_ready', {'rows': rows})['success'])
        with self.assertRaisesRegex(Exception, 'missing or ambiguous'):
            self._sxs_main_stock_control('remove', rows=rows)
        self.assertEqual(self.driver.execute_script('return window.removeCalls'), [])

    def test_extension_cart_ignores_only_removed_notices_and_rejects_unknown_rows(self):
        self._install_sxs_page_fixture()
        self.driver.execute_script("""
document.querySelector('#M_M_zCart').innerHTML='<div class="cR" id="cartRow_123_-1"><div class="cDes"><b>TT41</b> - Team 365 was removed</div></div>';
""")
        self.assertEqual(self._sxs_page_command('cart')['result']['rows'], [])
        self.driver.execute_script("document.querySelector('#M_M_zCart .cDes').textContent='Unreadable product'")
        result = self._sxs_page_command('cart')
        self.assertFalse(result['success'])
        self.assertIn('unreadable stock row', result['message'])

    def test_extension_checkout_recognizes_reduced_header_but_still_checks_account_details(self):
        self._install_sxs_page_fixture(path='/checkout/')
        self.driver.execute_script("document.querySelector('a[href*=signout]').remove()")
        self.assertTrue(self._sxs_page_command('health')['result']['signed_in'])
        self.assertTrue(self._sxs_page_command('health')['result']['checkout_ready'])
        self.assertTrue(self._sxs_page_command('review', {'po': '5000001'})['success'])
        self.driver.execute_script("document.querySelector('#shipping-address').textContent='Wrong address'")
        self.assertFalse(self._sxs_page_command('review', {'po': '5000001'})['success'])

    def test_extension_login_path_is_not_authenticated_by_its_cached_header(self):
        self._install_sxs_page_fixture(path='/myaccount/login')
        self.assertFalse(self._sxs_page_command('health')['result']['signed_in'])
        result = self._sxs_page_command('review', {'po': '5000001'})
        self.assertFalse(result['success'])
        self.assertIn('login expired', result['message'])

    def test_extension_confirmation_uses_the_visible_button_inside_the_order_popup(self):
        self._install_sxs_page_fixture()
        self.driver.execute_script("""
document.querySelector('#M_M_zConfirmOrderPopup_zCompleteBTN').style.display='none';
const root=document.querySelector('#M_M_zConfirmOrderPopup_PW-1');
const button=document.createElement('button');button.id='current-sxs-confirm';button.textContent='Confirm';button.onclick=()=>window.confirmClicks++;
root.append(button);
const unrelated=document.createElement('button');unrelated.textContent='Confirm';unrelated.onclick=()=>window.confirmClicks+=100;document.body.append(unrelated);
""")
        self.assertTrue(self._sxs_page_command('review', {'po': '5000001'})['success'])
        self.assertTrue(self._sxs_checkout_command('begin_submit', {'po': '5000001'})['success'])
        controls = self._sxs_page_command('health')['result']['confirmation_controls']
        self.assertEqual([c['id'] for c in controls], ['current-sxs-confirm'])
        self.assertTrue(self._sxs_checkout_command('confirm_submit', {'po': '5000001'})['success'])
        self.assertEqual(self.driver.execute_script('return window.confirmClicks'), 1)
        self.driver.execute_script("const b=document.createElement('button');b.textContent='YES';document.querySelector('#M_M_zConfirmOrderPopup_PW-1').append(b)")
        result = self._sxs_checkout_command('confirm_submit', {'po': '5000001'})
        self.assertFalse(result['success'])
        self.assertIn('ambiguous', result['message'])
        self.assertEqual(self.driver.execute_script('return window.confirmClicks'), 1)

    def test_extension_confirmation_treats_nested_devexpress_controls_as_one_button(self):
        self._install_sxs_page_fixture()
        self.driver.execute_script("""
const root=document.querySelector('#M_M_zConfirmOrderPopup_PW-1');
document.querySelector('#M_M_zConfirmOrderPopup_zCompleteBTN').remove();
const wrapper=document.createElement('div');wrapper.id='current-sxs-confirm';wrapper.className='dxbButton';
wrapper.innerHTML='<input type="submit" value="Confirm"><span>Confirm</span>';
wrapper.onclick=()=>window.confirmClicks++;root.append(wrapper);
""")
        self.assertTrue(self._sxs_page_command('review', {'po': '5000001'})['success'])
        self.assertTrue(self._sxs_checkout_command('begin_submit', {'po': '5000001'})['success'])
        self.assertTrue(self._sxs_checkout_command('confirm_submit', {'po': '5000001'})['success'])
        self.assertEqual(self.driver.execute_script('return window.confirmClicks'), 1)

    def test_extension_confirmation_uses_live_messagebox_while_legacy_popup_stays_hidden(self):
        self._install_sxs_page_fixture()
        self.driver.execute_script("""
document.querySelector('#M_M_zCheckoutBTN').onclick=()=>{
 window.completeClicks++;
 const overlay=document.createElement('div');overlay.className='messagebox_overlay';overlay.setAttribute('role','presentation');
 overlay.innerHTML='<div class="messagebox" role="dialog"><p>Are you sure you want to submit your order?</p><div class="messagebox_buttons"><button id="live-confirm">Confirm</button><button>Cancel</button></div></div>';
 document.body.append(overlay);overlay.querySelector('#live-confirm').onclick=()=>window.confirmClicks++;
};
const unrelated=document.createElement('div');unrelated.className='messagebox_overlay';
unrelated.innerHTML='<div class="messagebox" role="dialog"><p>Save unrelated preferences?</p><button>Confirm</button></div>';
unrelated.querySelector('button').onclick=()=>window.confirmClicks+=100;document.body.append(unrelated);
""")
        self.assertTrue(self._sxs_page_command('review', {'po': '5000001'})['success'])
        self.assertTrue(self._sxs_checkout_command('begin_submit', {'po': '5000001'})['success'])
        state = self._sxs_page_command('health')['result']
        self.assertEqual(state['extension_version'], '1.7.8')
        self.assertEqual([c['id'] for c in state['confirmation_controls']], ['live-confirm'])
        self.assertTrue(self._sxs_checkout_command('confirm_submit', {'po': '5000001'})['success'])
        self.assertEqual(self.driver.execute_script('return [window.completeClicks,window.confirmClicks]'), [1, 1])
        self.driver.execute_script("""
const extra=document.createElement('div');extra.className='messagebox_overlay';
extra.innerHTML='<div class="messagebox" role="dialog"><p>Are you sure you want to submit your order?</p><button>Confirm</button></div>';document.body.append(extra);
""")
        denied = self._sxs_checkout_command('confirm_submit', {'po': '5000001'})
        self.assertFalse(denied['success'])
        self.assertIn('ambiguous', denied['message'])
        self.assertEqual(self.driver.execute_script('return window.confirmClicks'), 1)

    def test_extension_confirmation_accepts_the_messagebox_without_an_aria_role(self):
        self._install_sxs_page_fixture()
        self.driver.execute_script("""
document.querySelector('#M_M_zCheckoutBTN').onclick=()=>{
 window.completeClicks++;
 const overlay=document.createElement('div');overlay.className='messagebox_overlay';
 overlay.innerHTML='<div class="messagebox"><p>Are you sure you want to submit your order?</p><button id="unlabelled-dialog-confirm">Confirm</button><button>Cancel</button></div>';
 document.body.append(overlay);overlay.querySelector('button').onclick=()=>window.confirmClicks++;
};
const other=document.createElement('div');other.className='messagebox_overlay';
other.innerHTML='<div class="messagebox"><p>Confirm some unrelated action?</p><button>Confirm</button></div>';
other.querySelector('button').onclick=()=>window.confirmClicks+=100;document.body.append(other);
""")
        self.assertTrue(self._sxs_page_command('review', {'po': '5000001'})['success'])
        self.assertTrue(self._sxs_checkout_command('begin_submit', {'po': '5000001'})['success'])
        state = self._sxs_page_command('health')['result']
        self.assertEqual([c['id'] for c in state['confirmation_controls']], ['unlabelled-dialog-confirm'])
        self.assertTrue(self._sxs_checkout_command('confirm_submit', {'po': '5000001'})['success'])
        self.assertEqual(self.driver.execute_script('return [window.completeClicks,window.confirmClicks]'), [1, 1])

    def test_extension_page_refuses_wrong_po_and_expired_or_unowned_mutations(self):
        self._install_sxs_page_fixture()
        denied = self._sxs_page_command('fill', {'allocations': []}, order_id='')
        self.assertFalse(denied['success'])
        self.assertIn('active Reorder', denied['message'])
        self.assertFalse(self._sxs_checkout_command('begin_submit', {'po': '5000002'})['success'])
        self.assertFalse(self._sxs_checkout_command('begin_submit', {'po': '5000001'}, deadline=1)['success'])
        self.assertTrue(self._sxs_page_command('review', {'po': '5000001'})['success'])
        self.assertTrue(self._sxs_checkout_command('begin_submit', {'po': '5000001'})['success'])
        self.driver.execute_script("document.querySelector('#M_M_zPONumberTB').value='5000002'")
        self.assertFalse(self._sxs_checkout_command('confirm_submit', {'po': '5000001'})['success'])
        self.assertEqual(self.driver.execute_script('return window.confirmClicks'), 0)

    def test_page_context_checkout_rechecks_po_address_shipping_and_payment_before_clicking(self):
        self._install_sxs_page_fixture()
        self.assertTrue(self._sxs_page_command('review', {'po': '5000001'})['success'])
        with self.assertRaisesRegex(Exception, 'active Reorder'):
            self._sxs_main_checkout_control('begin_submit', po='5000002')
        for change, restore in (
            ("document.querySelector('#shipping-address').textContent='Wrong address'", "document.querySelector('#shipping-address').textContent='2727 Commerce Way Philadelphia, PA 19154'"),
            ("document.querySelector('select[id$=zFreightServiceCB]').selectedIndex=0", "document.querySelector('select[id$=zFreightServiceCB]').selectedIndex=1"),
            ("document.querySelector('#M_M_zTermsCB').selectedIndex=0", "document.querySelector('#M_M_zTermsCB').selectedIndex=1"),
        ):
            self.driver.execute_script(change)
            with self.assertRaisesRegex(Exception, 'changed before the click'):
                self._sxs_main_checkout_control('begin_submit')
            self.driver.execute_script(restore)
        self.assertEqual(self.driver.execute_script('return [window.completeClicks,window.confirmClicks]'), [0, 0])

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
