import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from decimal import Decimal
from unittest.mock import MagicMock, patch

from workers import crm_sleeve_prints as extra
from workers import crm_reverse_prints as reverse


class ExtraEmbTests(unittest.TestCase):
    def plan(self, descriptions, price=None):
        request = extra.normalize_request([{"tab_number": 1, "quantity": 10, "extra_emb": "embroidery"}], embroidery_price=price)
        return extra._build_live_plan(request, {"designs": [{"tab_number": 1, "quantity": 10,
            "print_areas": [{"description": name, "method": "Embroidery"} for name in descriptions]}]})

    def test_area_conversion_reuse_and_one_charge_per_garment(self):
        for names in (["Front"], ["Front-left chest"], ["Front-right chest"],
                      ["Front-left chest", "Front-right chest"], ["Back", "Front"],
                      ["Front", "Front-left chest"], ["Front", "Front-right chest"]):
            with self.subTest(names=names):
                plan = self.plan(names, "12.50")
                self.assertEqual(plan["selections"][0]["surcharge"], Decimal("12.50"))
                with patch.object(extra.shared, "_order_scope", return_value={}) as scope:
                    extra._apply_crm_sleeve_changes(None, plan, "Emb note")
                script, payload = scope.call_args.args[1:]
                harness = '''
const r = {designs:[{printAreas:NAMES.map((description,index)=>({description,methodDescription:'HD Digital',artwork:index})),
designItems:[{splitIntoSizes:0,pricePerPiece:'10.00'},
{splitIntoSizes:1,sizes:[{quantity:2,pricePerPiece:'20.00'},{quantity:0,pricePerPiece:'9.00'}]}]}]};
const s = {editMode:true,STATICS:{printAreaTemplates:['Front - Left Chest','Front - Right Chest'].map(description=>({description})),
printMethods:[{description:'Embroidery'}]},addPrintArea:d=>d.printAreas.push({}),
updateAreaTemplate:(a,t)=>a.description=t.description,updateAreaMethod:(a,m)=>a.methodDescription=m.description};
const result = new Function('s','r','runInAngular','return function(){'+SCRIPT+'}')(s,r,(_,action)=>action())(PAYLOAD);
process.stdout.write(JSON.stringify({result,design:r.designs[0]}));
'''.replace("NAMES", json.dumps(names)).replace("SCRIPT", json.dumps(script)).replace("PAYLOAD", json.dumps(payload))
                output = json.loads(subprocess.check_output(["node", "-e", harness], text=True))
                areas = output["design"]["printAreas"]
                chest = [a for a in areas if "chest" in a["description"].lower()]
                self.assertEqual(len(chest), 2)
                self.assertTrue(all(a["methodDescription"] == "Embroidery" for a in chest))
                self.assertEqual(areas[0]["artwork"], 0)
                if "Back" in names:
                    self.assertEqual(areas[0]["description"], "Back")
                    self.assertEqual(areas[0]["methodDescription"], "HD Digital")
                items = output["design"]["designItems"]
                self.assertEqual(items[0]["pricePerPiece"], "22.50")
                self.assertEqual(items[1]["sizes"][0]["pricePerPiece"], "32.50")
                self.assertEqual(items[1]["sizes"][1]["pricePerPiece"], "9.00")

    def test_default_wording_and_mixed_categories(self):
        plan = self.plan(["Front"])
        self.assertEqual(plan["embroidery_price"], Decimal("15"))
        self.assertEqual(plan["ink_quantity"], 0)
        selections = plan["selections"]
        self.assertEqual(extra.format_sales_note(selections, None, Decimal("15")),
                         "Additional embroidery area\n$15.00 each\nemailed txted")
        self.assertEqual(extra._format_request_text(selections), "additional embroidery area")
        self.assertEqual(extra._format_cost_text(selections, None, Decimal("15")), "$15.00")
        mixed = selections + [{"left": "ink"}, {"reverse": "ink"}]
        self.assertEqual(extra._format_request_text(mixed), "sleeve prints and reverse prints and additional embroidery area")
        self.assertIn("$12.50 each", extra.format_sales_note(mixed, Decimal("8"), Decimal("12.5"), Decimal("7")))

    def test_reject_invalid_category_and_ambiguous_areas(self):
        for fields in ({"extra_emb": "ink"}, {"extra_emb": "embroidery", "left": "ink"},
                       {"extra_emb": "embroidery", "reverse": "ink"}):
            with self.assertRaises(extra.SleevePrintsError):
                extra.normalize_request([{"tab_number": 1, "quantity": 10, **fields}])
        for areas in ([], ["Back"], ["Front", "Front"], ["Front", "Front-left chest", "Front-right chest"]):
            with self.assertRaises(extra.SleevePrintsError):
                self.plan(areas)

    def test_retry_verifies_prices_and_sends_email_once(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "_switch_to_crm_app_frame",
                         "_wait_for_order_scope", "safe_driver_quit", "safe_take_screenshot", "_save_order_and_wait"):
                stack.enter_context(patch.object(extra.shared, name))
            stack.enter_context(patch.object(extra.shared, "_open_driver", return_value=MagicMock()))
            stack.enter_context(patch.object(extra.shared, "_wait_for_crm_contact_info", return_value={"email": "customer@example.test"}))
            # An identical note from a different tab must not suppress this tab's charge.
            state = {"sales_notes": "Additional embroidery area\n$15.00 each\nemailed txted", "designs": [
                {"tab_number": 1, "quantity": 10, "print_areas": [{"description": "Front", "method": "Embroidery"}]}]}
            stack.enter_context(patch.object(extra, "_read_crm_sleeve_state", return_value=state))
            apply = stack.enter_context(patch.object(extra, "_apply_crm_sleeve_changes", return_value={"price_updates": []}))
            verify = stack.enter_context(patch.object(extra, "_verify_crm_sleeve_changes", return_value={}))
            stack.enter_context(patch.object(extra, "_capture_view_invoice_link", return_value="https://example.test/invoice"))
            send = stack.enter_context(patch.object(extra, "_prepare_and_send_salesforce_email", return_value={"sent": True}))
            stack.enter_context(patch.object(reverse, "STATE_DIR", directory))
            selection = [{"tab_number": 1, "quantity": 10, "extra_emb": "embroidery"}]
            self.assertTrue(extra.process_sleeve_prints_order("5209138", selection)["success"])
            self.assertTrue(extra.process_sleeve_prints_order("5209138", selection)["success"])
            self.assertEqual(apply.call_count, 1)
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(send.call_count, 1)
            verify.side_effect = extra.SleevePrintsError("Saved prices do not match")
            with self.assertRaises(extra.SleevePrintsError):
                extra.process_sleeve_prints_order("5209138", selection)
            self.assertEqual(apply.call_count, 1)
            self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
