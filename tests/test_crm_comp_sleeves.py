import json
import subprocess
import unittest
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from workers import crm_sleeve_prints as sleeves
from workers import salesforce_activity_confirmation as confirmation


def live_plan(selections, **prices):
    request = sleeves.normalize_request(selections, **prices)
    state = {"designs": [
        {"tab_number": selection["tab_number"], "quantity": 10,
         "print_areas": [{"description": "Front", "method": "HD Digital"}]}
        for selection in selections
    ]}
    return sleeves._build_live_plan(request, state)


class CompSleevesPricingTests(unittest.TestCase):
    def test_zero_sleeve_prices_select_comp_template_for_each_method_and_location(self):
        for method, prices in (("ink", {"ink_price": 0}),
                               ("embroidery", {"embroidery_price": "0.00"})):
            for areas in ({"left": method}, {"right": method}, {"left": method, "right": method}):
                with self.subTest(method=method, areas=areas):
                    plan = live_plan([{"tab_number": 1, "quantity": 10, **areas}], **prices)
                    self.assertEqual(plan["selections"][0]["surcharge"], Decimal("0.00"))
                    self.assertEqual(sleeves._salesforce_process_for_plan(plan).salesforce_template,
                                     "[AUTO] Comp Sleeves")
        plan = live_plan([
            {"tab_number": 1, "quantity": 10, "left": "ink"},
            {"tab_number": 2, "quantity": 10, "right": "embroidery"},
        ], ink_price="0", embroidery_price="0")
        self.assertEqual(sleeves._salesforce_process_for_plan(plan), sleeves.COMP_SLEEVES_PROCESS)

    def test_paid_sleeves_and_other_categories_keep_additional_requests(self):
        cases = [
            ([{"left": "ink"}], {}),
            ([{"right": "embroidery"}], {"embroidery_price": "0.01"}),
            ([{"left": "ink", "right": "embroidery"}], {"ink_price": 0}),
            ([{"side_left": "ink"}], {"ink_price": 0}),
            ([{"extra_emb": "embroidery"}], {"embroidery_price": 0}),
            ([{"reverse": "ink"}], {"reverse_price": 0}),
            ([{"left": "ink"}, {"side_right": "ink"}], {"ink_price": 0}),
            ([{"left": "ink"}, {"reverse": "ink"}], {"ink_price": 0, "reverse_price": 0}),
        ]
        for areas, prices in cases:
            with self.subTest(areas=areas, prices=prices):
                plan = live_plan([
                    {"tab_number": index + 1, "quantity": 10, **area}
                    for index, area in enumerate(areas)
                ], **prices)
                self.assertEqual(sleeves._salesforce_process_for_plan(plan), sleeves.SLEEVE_PRINTS_PROCESS)

    def test_comp_sleeves_add_areas_without_writing_prices_and_verify_unchanged_prices(self):
        plan = live_plan([{"tab_number": 1, "quantity": 10, "left": "ink", "right": "embroidery"}],
                         ink_price=0, embroidery_price=0)
        with patch.object(sleeves.shared, "_order_scope", return_value={}) as scope:
            sleeves._apply_crm_sleeve_changes(None, plan, "Comp sleeve note")
        script, payload = scope.call_args.args[1:]
        harness = '''
const r = {designs:[{printAreas:[{description:'Sleeve Left',methodDescription:'Screen Printing'}],
designItems:[{splitIntoSizes:0,pricePerPiece:'10'},
{splitIntoSizes:1,sizes:[{quantity:2,pricePerPiece:'20.25'},
{quantity:0,pricePerPiece:'9.00'}]}]}]};
const before = JSON.stringify(r.designs[0].designItems);
const s = {editMode:true,STATICS:{printAreaTemplates:['Sleeve Left','Sleeve Right'].map(description=>({description})),
printMethods:['HD Digital','Embroidery'].map(description=>({description}))},
addPrintArea:d=>d.printAreas.push({}),updateAreaTemplate:(a,t)=>a.description=t.description,
updateAreaMethod:(a,m)=>a.methodDescription=m.description,
watchItemChanges:()=>{throw new Error('Unexpected product price write');},
watchSizeChanges:()=>{throw new Error('Unexpected size price write');}};
const result = new Function('s','r','runInAngular','return function(){'+SCRIPT+'}')(s,r,(_,fn)=>fn())(PAYLOAD);
process.stdout.write(JSON.stringify({result,order:r,unchanged:before===JSON.stringify(r.designs[0].designItems)}));
'''.replace("SCRIPT", json.dumps(script)).replace("PAYLOAD", json.dumps(payload))
        output = json.loads(subprocess.check_output(["node", "-e", harness], text=True))
        self.assertTrue(output["unchanged"])
        self.assertEqual(output["result"]["added_areas"], [
            {"tab_number": 1, "description": "Sleeve Right", "method": "Embroidery"}])
        self.assertEqual(output["result"]["existing_areas"][0]["description"], "Sleeve Left")
        self.assertEqual([p["price"] for p in output["result"]["price_updates"]], ["10", "20.25"])
        with patch.object(sleeves.shared, "_order_scope", return_value={
            "note_saved": True, "missing_areas": [], "incorrect_prices": []
        }) as verify:
            sleeves._verify_crm_sleeve_changes(None, "Comp sleeve note", output["result"])
        self.assertEqual(verify.call_args.args[4], output["result"]["price_updates"])
        with patch.object(sleeves.shared, "_order_scope", return_value={
            "note_saved": True, "incorrect_prices": [{"price": "10", "actual": "11"}]
        }), self.assertRaisesRegex(sleeves.SleevePrintsError, "prices were not confirmed"):
            sleeves._verify_crm_sleeve_changes(None, "Comp sleeve note", output["result"])

    def test_extension_keeps_explicit_zero_in_price_summary(self):
        content = (Path(__file__).resolve().parents[1] / "crm-order-dark-mode-extension/content.js").read_text(encoding="utf-8")
        functions = content[content.index("const EXTRA_PRINT_AREAS"):content.index("function showSleevePrintsDialog")]
        output = subprocess.check_output(["node", "-e", functions + '''
const price = sleevePrintCleanPrice('0.00');
const summary = sleevePrintSelectionSummary([{tab_number:1,left:'ink',right:'embroidery'}],
new Map([[1,{quantity:10}]]),price,price);
process.stdout.write(JSON.stringify({price,summary}));
'''], text=True)
        result = json.loads(output)
        self.assertTrue(result["price"]["valid"])
        self.assertEqual(result["summary"]["inkPrice"], 0)
        self.assertEqual(result["summary"]["embroideryPrice"], 0)


class CompSleevesEmailTests(unittest.TestCase):
    def test_comp_placeholder_replacement_allows_absent_tokens_but_paid_template_stays_strict(self):
        driver = Mock()
        driver.execute_script.return_value = {"[REQUEST]": 0, "[COST]": 0, "[INVOICE_LINK]": 0}
        counts = sleeves._replace_additional_request_placeholders(
            driver, "sleeve prints", "$0.00", "https://example.test/invoice", required=False)
        self.assertEqual(counts, driver.execute_script.return_value)
        with self.assertRaisesRegex(sleeves.SleevePrintsError, "two.*placeholders"):
            sleeves._replace_additional_request_placeholders(
                driver, "sleeve prints", "$5.00", "https://example.test/invoice")

    def email_mocks(self, stack, initial_body, final_body, replacements=None):
        for name in ("_open_salesforce_account", "_verify_salesforce_email", "_click_salesforce_email",
                     "_wait_for_email_composer", "_set_salesforce_from_orders", "_replace_subject_order_number"):
            stack.enter_context(patch.object(sleeves.shared, name))
        template = stack.enter_context(patch.object(sleeves.shared, "_insert_cancel_template"))
        subject = "Complimentary Sleeves - [ORDER-NUMBER]"
        stack.enter_context(patch.object(sleeves.shared, "_read_salesforce_email_state", side_effect=[
            {"subject": subject, "body": initial_body},
            {"subject": "Complimentary Sleeves - 1234567", "body": final_body},
        ]))
        replace = stack.enter_context(patch.object(sleeves, "_replace_additional_request_placeholders",
                                                   return_value=replacements or {}))
        stack.enter_context(patch.object(sleeves, "_verify_final_recipients"))
        stack.enter_context(patch.object(sleeves.time, "sleep"))
        send = stack.enter_context(patch.object(confirmation, "send_and_confirm", return_value={"activity_verified": True}))
        return template, replace, send

    def send(self, dry_run=False):
        return sleeves._prepare_and_send_salesforce_email(
            Mock(), "crm", "1234567", "customer@example.test", "sleeve prints", "$0.00",
            "https://example.test/invoice", process=sleeves.COMP_SLEEVES_PROCESS, dry_run=dry_run)

    def test_comp_template_does_not_require_paid_placeholders_and_confirms_activity(self):
        body = "We have added the requested print areas at no additional charge."
        with ExitStack() as stack:
            template, replace, send = self.email_mocks(stack, body, body)
            result = self.send()
            template.assert_called_once_with(ANY, sleeves.COMP_SLEEVES_PROCESS)
            self.assertFalse(replace.call_args.kwargs["required"])
            sleeves.shared._replace_subject_order_number.assert_called_once_with(ANY, "1234567")
            send.assert_called_once()
        self.assertTrue(result["sent"])
        self.assertTrue(result["activity_verified"])
        self.assertEqual(result["template"], "[AUTO] Comp Sleeves")

    def test_comp_optional_placeholders_are_replaced_and_verified(self):
        body = "Your sleeve [REQUEST] cost [COST]. [INVOICE_LINK]"
        final = "Your sleeve sleeve prints cost $0.00. https://example.test/invoice"
        counts = {"[REQUEST]": 1, "[COST]": 1, "[INVOICE_LINK]": 1}
        with ExitStack() as stack:
            self.email_mocks(stack, body, final, counts)
            self.assertTrue(self.send()["sent"])
        with ExitStack() as stack:
            _, _, send = self.email_mocks(stack, body, "Your sleeve sleeve prints cost $15.00.", counts)
            with self.assertRaisesRegex(sleeves.SleevePrintsError, "cost and invoice link"):
                self.send()
            send.assert_not_called()

    def test_unresolved_placeholder_or_unconfirmed_activity_stops_comp_email(self):
        body = "Your complimentary sleeve prints have been added."
        with ExitStack() as stack:
            _, _, send = self.email_mocks(stack, body, body + " [COST]")
            with self.assertRaisesRegex(sleeves.SleevePrintsError, "unresolved placeholders"):
                self.send()
            send.assert_not_called()
        with ExitStack() as stack:
            _, _, send = self.email_mocks(stack, body, body)
            send.side_effect = confirmation.UnconfirmedEmailError("Activity unconfirmed")
            with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, "Activity unconfirmed"):
                self.send()

    def test_dry_run_does_not_send_comp_email(self):
        body = "Your complimentary sleeve prints have been added."
        with ExitStack() as stack:
            _, _, send = self.email_mocks(stack, body, body)
            result = self.send(dry_run=True)
            send.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertTrue(result["dry_run"])


class CompSleevesWorkflowTests(unittest.TestCase):
    def test_existing_zero_price_note_still_checks_requested_areas_before_comp_email(self):
        with ExitStack() as stack:
            for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "_switch_to_crm_app_frame",
                         "_wait_for_order_scope", "safe_driver_quit", "safe_take_screenshot", "_save_order_and_wait"):
                stack.enter_context(patch.object(sleeves.shared, name))
            stack.enter_context(patch.object(sleeves.shared, "_open_driver", return_value=Mock()))
            stack.enter_context(patch.object(sleeves.shared, "_wait_for_crm_contact_info",
                                             return_value={"email": "customer@example.test"}))
            state = {"sales_notes": "Sleeve prints\nPriced at $0.00 per sleeve\nEmailed Txted", "designs": [
                {"tab_number": 1, "quantity": 10, "print_areas": []}]}
            stack.enter_context(patch.object(sleeves, "_read_crm_sleeve_state", return_value=state))
            apply = stack.enter_context(patch.object(sleeves, "_apply_crm_sleeve_changes", return_value={}))
            verify = stack.enter_context(patch.object(sleeves, "_verify_crm_sleeve_changes", return_value={}))
            stack.enter_context(patch.object(sleeves, "_capture_view_invoice_link", return_value="https://example.test/invoice"))
            send = stack.enter_context(patch.object(sleeves, "_prepare_and_send_salesforce_email", return_value={"sent": True}))
            selection = [{"tab_number": 1, "quantity": 10, "left": "ink", "right": "ink"}]
            result = sleeves.process_sleeve_prints_order("1234567", selection, ink_price=0)
            self.assertTrue(result["success"])
            self.assertEqual(result["salesforce_template"], "[AUTO] Comp Sleeves")
            self.assertEqual(apply.call_args.args[1]["selections"][0]["right"], "ink")
            verify.assert_called_once()
            self.assertEqual(sleeves.shared.safe_get_with_partial_load.call_count, 2)
            self.assertIn("Verify saved sleeve areas", sleeves.shared.safe_get_with_partial_load.call_args.args[2])
            self.assertEqual(send.call_args.kwargs["process"], sleeves.COMP_SLEEVES_PROCESS)
            verify.side_effect = sleeves.SleevePrintsError("Sleeve areas were not saved")
            with self.assertRaisesRegex(sleeves.SleevePrintsError, "Sleeve areas were not saved"):
                sleeves.process_sleeve_prints_order("1234567", selection, ink_price=0)
            send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
