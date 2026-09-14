import copy
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, MagicMock

from workers import crm_sleeve_prints as extra
from workers import crm_reverse_prints as reverse


def source_design(index=0):
    return {"index": index, "id": str(index + 10), "design_id": "art-100", "name": "Original",
            "areas": [{"description": "Front", "method": "HD Digital", "artwork": "a"}],
            "items": [{"id": "item1", "is_style_sub": False, "catalog_style": "ST5000",
                       "vendor": "Sanmar", "style": "ST5000", "color": "Black/ Deep Red",
                       "description": "Reversible Mesh Tank", "split": True, "quantity": 8, "price": "33.09",
                       "sizes": [{"label": "M", "quantity": 5, "price": "33.09"},
                                 {"label": "L", "quantity": 2, "price": "33.09"},
                                 {"label": "XL", "quantity": 1, "price": "33.09"}]}]}


def clone_design(source, index=1, price="8.00"):
    clone = copy.deepcopy(source)
    clone.update(index=index, id=str(index + 10), name="REVERSE-PRINT")
    for item in clone["items"]:
        item.update(is_style_sub=True, catalog_style="Style Sub", color=reverse.reverse_color(item["color"]))
        for size in item["sizes"]:
            size["price"] = price
    return clone


class ReversePriceTests(unittest.TestCase):
    def test_extension_background_and_http_bridge_preserve_reverse_override(self):
        root = Path(__file__).resolve().parents[1] / "crm-order-dark-mode-extension"
        bridge = (root / "bridge.js").read_text(encoding="utf-8").replace("export ", "")
        background = (root / "background.js").read_text(encoding="utf-8")
        start = background.index("chrome.runtime.onMessage.addListener")
        listener = background[start:background.index("\n});", start) + 4]
        harness = '''
let handler, posted;
const chrome = {runtime:{onMessage:{addListener: fn => handler=fn}}};
global.fetch = async (url,options) => {posted=JSON.parse(options.body);return {ok:true,json:async()=>({success:true})}};
''' + bridge + listener + '''
handler({type:'crm-order-automation:manual-start',orderId:'5209138',automation:'sleeve_prints',
 sleeves:[{tab_number:1,quantity:8,reverse:'ink'}],reverse_price:4.25},null,()=>process.stdout.write(JSON.stringify(posted)));
'''
        posted = json.loads(subprocess.check_output(["node", "-e", harness], text=True))
        self.assertEqual(posted["reverse_price"], 4.25)
        self.assertEqual(posted["sleeves"][0]["reverse"], "ink")

    def test_selected_garments_set_tier_and_front_back_multiply_shared_override(self):
        request = extra.normalize_request([
            {"tab_number": 1, "quantity": 8, "reverse": "ink"},
            {"tab_number": 2, "quantity": 2, "reverse": "ink"}], reverse_price="4.25")
        state = {"designs": [
            {"tab_number": 1, "quantity": 8, "print_areas": [{"description": "Front", "method": "HD Digital"}]},
            {"tab_number": 2, "quantity": 2, "print_areas": [{"description": name, "method": "Screen Printing"} for name in ("Front", "Back")]},
            {"tab_number": 3, "quantity": 500, "print_areas": []}]}
        plan = extra._build_live_plan(request, state)
        self.assertEqual(plan["ink_quantity"], 10)
        self.assertEqual([x["reverse_unit_price"] for x in plan["selections"]], [Decimal("4.25"), Decimal("8.50")])
        plan = extra._build_live_plan({**request, "reverse_price": None}, state)
        self.assertEqual(plan["reverse_price"], Decimal("7"))
        self.assertEqual([x["surcharge"] for x in plan["selections"]], [Decimal("0"), Decimal("0")])

    def test_reverse_cannot_be_embroidery_or_combined_with_another_category(self):
        for fields in ({"reverse": "embroidery"}, {"reverse": "ink", "left": "ink"}, {"reverse": True}):
            with self.subTest(fields=fields), self.assertRaises(extra.SleevePrintsError):
                extra.normalize_request([{"tab_number": 1, "quantity": 8, **fields}])
        for price in ("NaN", "Infinity", "-1"):
            with self.assertRaises(extra.SleevePrintsError):
                extra.normalize_request([{"tab_number": 1, "quantity": 8, "reverse": "ink"}], reverse_price=price)

    def test_reverse_rejects_unknown_or_embroidery_areas_before_mutation(self):
        request = extra.normalize_request([{"tab_number": 1, "quantity": 8, "reverse": "ink"}])
        for areas in ([], [{"description": "Front", "method": "Embroidery"}], [{"description": "Sleeve Left", "method": "HD Digital"}]):
            with self.assertRaises(extra.SleevePrintsError):
                extra._build_live_plan(request, {"designs": [{"tab_number": 1, "quantity": 8, "print_areas": areas}]})

    def test_reverse_note_and_template_replacements(self):
        selection = [{"reverse": "ink"}]
        self.assertEqual(extra._format_request_text(selection), "reverse prints")
        self.assertEqual(extra._format_cost_text(selection, None, None, Decimal("8")), "$8.00 per area")
        self.assertEqual(extra.format_sales_note(selection, None, None, Decimal("8")),
                         "Reverse prints\nPriced at $8.00 per area\nEmailed Txted")

    def test_browser_tier_counts_selected_tabs_once_and_override_is_separate(self):
        content = (Path(__file__).resolve().parents[1] / "crm-order-dark-mode-extension/content.js").read_text(encoding="utf-8")
        functions = content[content.index("const EXTRA_PRINT_AREAS"):content.index("function showSleevePrintsDialog")]
        result = json.loads(subprocess.check_output(["node", "-e", functions + '''
const selected = [{tab_number:1,reverse:'ink'}, {tab_number:2,reverse:'ink'}, {tab_number:3,left:'ink',right:'ink'}];
const tabs = new Map([[1,{quantity:8}],[2,{quantity:2}],[3,{quantity:10}],[4,{quantity:1000}]]);
process.stdout.write(JSON.stringify(sleevePrintSelectionSummary(selected,tabs,{value:null},{value:null},{value:4.25})));
'''], text=True))
        self.assertEqual(result["inkQuantity"], 20)
        self.assertEqual(result["inkPrice"], 6)
        self.assertEqual(result["reversePrice"], 4.25)


class ReverseCloneTests(unittest.TestCase):
    def test_source_waits_for_inventory_vendor_before_validation(self):
        source = source_design()
        pending = copy.deepcopy(source)
        pending["items"][0]["vendor"] = ""
        with patch.object(reverse, "read_designs", side_effect=[[pending], [source]]) as read, patch.object(reverse.time, "sleep") as sleep:
            result = reverse._wait_for_source_vendors(None, [{"tab_number": 1}])
        self.assertEqual(result, [source])
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_vendor_wait_times_out_without_assuming_a_vendor(self):
        pending = source_design()
        pending["items"][0]["vendor"] = ""
        with patch.object(reverse, "read_designs", return_value=[pending]), self.assertRaisesRegex(reverse.ReversePrintError, "after waiting for inventory rows"):
            reverse._wait_for_source_vendors(None, [{"tab_number": 1}], timeout=0)

    def test_style_sub_without_details_is_a_resumable_clone(self):
        source = source_design()
        clone = clone_design(source)
        clone["items"][0].update(style="", color="", vendor="", description="")
        self.assertIs(reverse.find_clone(source, [source, clone]), clone)

    def test_colors_reverse_and_single_color_stays(self):
        self.assertEqual(reverse.reverse_color("Black/ Deep Red"), "Deep Red/ Black")
        self.assertEqual(reverse.reverse_color("White"), "White")
        for color in ("", "Black/", "A/B/C"):
            with self.assertRaises(reverse.ReversePrintError):
                reverse.reverse_color(color)

    def test_complete_clone_verifies_even_when_size_columns_change(self):
        source = source_design()
        clone = clone_design(source)
        clone["items"][0]["sizes"].reverse()
        reverse._assert_clone(source, clone, "8")
        self.assertIs(reverse.find_clone(source, [source, clone]), clone)

    def test_partial_clone_matches_but_wrong_details_fail_verification(self):
        source = source_design()
        clone = clone_design(source)
        clone["items"][0]["color"] = "Black/ Deep Red"
        clone["items"][0]["sizes"][0]["quantity"] = 4
        self.assertIs(reverse.find_clone(source, [source, clone]), clone)
        with self.assertRaises(reverse.ReversePrintError):
            reverse._assert_clone(source, clone, "8")
        with self.assertRaises(reverse.ReversePrintError):
            reverse.find_clone(source, [source, clone, copy.deepcopy(clone)])

    def test_each_product_is_checked_and_source_prices_must_not_change(self):
        source = source_design()
        source["items"].append(copy.deepcopy(source["items"][0]))
        source["items"][1]["color"] = "White"
        clone = clone_design(source)
        reverse._assert_clone(source, clone, "8")
        clone["items"][1]["sizes"][0]["price"] = "33.09"
        with self.assertRaises(reverse.ReversePrintError):
            reverse._assert_clone(source, clone, "8")
        clone = clone_design(source)
        changed_source = copy.deepcopy(source)
        changed_source["items"][0]["sizes"][0]["price"] = "8"
        with patch.object(reverse, "read_designs", return_value=[changed_source, clone]), self.assertRaises(reverse.ReversePrintError):
            reverse.verify_reverse_prints(None, {"jobs": [{"source": source, "clone_index": 1, "unit_price": "8"}]})

    def test_crm_size_restore_uses_labels_and_not_column_positions(self):
        source = source_design()["items"][0]
        with patch.object(reverse.shared, "_order_scope", return_value=True) as scope:
            reverse._restore_sizes(None, 0, 0, source, "16.00")
        script = scope.call_args.args[1]
        harness = '''
const item = {splitIntoSizes:1, sizes:[
 {size:'XS',quantity:0,pricePerPiece:'0'}, {size:'S',quantity:5,pricePerPiece:'33.09'},
 {size:'M',quantity:2,pricePerPiece:'33.09'}, {size:'L',quantity:1,pricePerPiece:'33.09'},
 {size:'XL',quantity:0,pricePerPiece:'0'}]};
const r = {designs:[{designItems:[item]}]}, changes=[];
const s = {watchSizeQuantityChanges:(item,size)=>changes.push(size.size), watchSizeChanges:()=>{}};
const runInAngular = (s,fn)=>fn();
new Function('s','r','runInAngular','return function(){'+SCRIPT+'}')(s,r,runInAngular)(0,0,SOURCE,'16.00');
process.stdout.write(JSON.stringify(item.sizes));
'''.replace("SCRIPT", json.dumps(script)).replace("SOURCE", json.dumps(source))
        result = json.loads(subprocess.check_output(["node", "-e", harness], text=True))
        self.assertEqual([(x["size"], x["quantity"]) for x in result], [("XS", 0), ("S", 0), ("M", 5), ("L", 2), ("XL", 1)])
        self.assertEqual([x["pricePerPiece"] for x in result if x["quantity"]], ["16.00"] * 3)


class ReverseWorkflowTests(unittest.TestCase):
    def _pipeline(self, stack, directory):
        driver = MagicMock()
        for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "_switch_to_crm_app_frame",
                     "_wait_for_order_scope", "safe_driver_quit", "safe_take_screenshot", "_save_order_and_wait"):
            stack.enter_context(patch.object(extra.shared, name))
        stack.enter_context(patch.object(extra.shared, "_open_driver", return_value=driver))
        stack.enter_context(patch.object(extra.shared, "_wait_for_crm_contact_info", return_value={"email": "customer@example.test"}))
        state = {"sales_notes": "Reverse prints\nPriced at $8.00 per area\nEmailed Txted", "designs": [
            {"tab_number": 1, "quantity": 8, "print_areas": [{"description": "Front", "method": "HD Digital"}]}]}
        stack.enter_context(patch.object(extra, "_read_crm_sleeve_state", return_value=state))
        stack.enter_context(patch.object(extra, "_apply_crm_sleeve_changes", return_value={}))
        stack.enter_context(patch.object(extra, "_verify_crm_sleeve_changes", return_value={}))
        stack.enter_context(patch.object(extra, "_capture_view_invoice_link", return_value="https://example.test/invoice"))
        send = stack.enter_context(patch.object(extra, "_prepare_and_send_salesforce_email", return_value={"sent": True}))
        mutation = {"jobs": [{"source": source_design(), "clone_index": 1, "unit_price": "8"}]}
        apply = stack.enter_context(patch.object(reverse, "apply_reverse_prints", return_value=mutation))
        verify = stack.enter_context(patch.object(reverse, "verify_reverse_prints", return_value=True))
        stack.enter_context(patch.object(reverse, "STATE_DIR", directory))
        return apply, verify, send

    def test_saved_note_does_not_skip_clone_verification_but_sent_receipt_skips_email(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            apply, verify, send = self._pipeline(stack, directory)
            selection = [{"tab_number": 1, "quantity": 8, "reverse": "ink"}]
            first = extra.process_sleeve_prints_order("5209138", selection)
            second = extra.process_sleeve_prints_order("5209138", selection)
            self.assertTrue(first["success"])
            self.assertTrue(second["salesforce"]["skipped"])
            self.assertEqual(apply.call_count, 2)
            self.assertEqual(verify.call_count, 2)
            send.assert_called_once()

    def test_clone_verification_failure_prevents_email_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            apply, verify, send = self._pipeline(stack, directory)
            verify.side_effect = reverse.ReversePrintError("Wrong clone price")
            with self.assertRaisesRegex(extra.SleevePrintsError, "Wrong clone price"):
                extra.process_sleeve_prints_order("5209138", [{"tab_number": 1, "quantity": 8, "reverse": "ink"}])
            send.assert_not_called()
            self.assertEqual(list(Path(directory).rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
