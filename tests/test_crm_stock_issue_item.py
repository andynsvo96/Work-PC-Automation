from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import server
from workers import crm_stock_issue_item as item


def product(**changes):
    return {"style": "DM130", "description": "District Perfect Tri Tee", "color": "Red",
            "design_item_ids": ["design-item-101"], "tab_numbers": [1], "total_quantity": 4, **changes}


def snapshot():
    return {"id": "1234567", "designs": [{"items": [
        {"id": "101", "style": "DM130", "description": "District Perfect Tri Tee", "color": "Red",
         "quantity": 4, "split": True, "sizes": [{"label": "M", "quantity": 4}]},
        {"id": "102", "style": "5000", "description": "Cotton Tee", "color": "Black",
         "quantity": 3, "split": False},
    ]}]}


class StockIssueItemTests(unittest.TestCase):
    def request(self):
        return item.normalize_request("5000 Black Gildan Cotton Tee", [product()])

    def test_requires_replacement_and_scanned_stock_identity(self):
        for replacement in (None, "", " ", [], "<b>Tee</b>", "[PRODUCT]", "a\nb", "a" * 501):
            with self.subTest(replacement=replacement), self.assertRaises(item.StockIssueItemError):
                item.normalize_request(replacement, [product()])
        for products in ([], [product(design_item_ids=[])], [product(total_quantity=None)],
                         [product(), product(style="Other")]):
            with self.subTest(products=products), self.assertRaises(item.StockIssueItemError):
                item.normalize_request("Replacement", products)
        request = item.normalize_request("  Soft & Light,  Navy  ", [product()])
        self.assertEqual(request["product"], "Soft & Light, Navy")

    def test_every_selected_row_requires_its_own_replacement(self):
        first = product(replacement_product=' Soft & Light,  Navy ')
        second = product(style='5000', description='Cotton Tee', color='Black',
                         design_item_ids=['design-item-102'], total_quantity=3, replacement_product='Performance Tee')
        request = item.normalize_request(None, [first, second])
        self.assertNotIn('product', request)
        self.assertEqual([row['replacement_product'] for row in request['products']],
                         ['Soft & Light, Navy', 'Performance Tee'])
        for missing in (None, '', ' ', '<b>Tee</b>', '[PRODUCT]'):
            with self.subTest(missing=missing), self.assertRaises(item.StockIssueItemError):
                item.normalize_request('A global fallback must not fill a missing row',
                                       [first, {**second, 'replacement_product': missing}])
        with self.assertRaises(item.StockIssueItemError):
            item.normalize_request('Global fallback', [first, product()])
        with self.assertRaisesRegex(item.StockIssueItemError, 'duplicate product rows'):
            item.normalize_request(None, [first, {**first, 'design_item_ids': ['design-item-201']}])

    def test_one_two_and_three_items_keep_corresponding_email_list_order(self):
        rows = [
            product(style='P1', description='First Tee', color='Black', replacement_product='Performance Tee'),
            product(style='P2', description='Second Hoodie', color='Navy',
                    design_item_ids=['design-item-102'], replacement_product='Zip Hoodie'),
            product(style='P3', description='Third Cap', color='White',
                    design_item_ids=['design-item-103'], replacement_product='Trucker Cap'),
        ]
        expected = [
            ('P1 black First Tee', 'Performance Tee'),
            ('P1 black First Tee and P2 navy Second Hoodie', 'Performance Tee and Zip Hoodie'),
            ('P1 black First Tee, P2 navy Second Hoodie, and P3 white Third Cap',
             'Performance Tee, Zip Hoodie, and Trucker Cap'),
        ]
        for count, (stock, replacement) in enumerate(expected, 1):
            with self.subTest(count=count):
                request = item.normalize_request(None, rows[:count])
                self.assertEqual(item.placeholder_values('1234567', request), {
                    '[ORDER-NUMBER]': '1234567', '[STOCK]': stock, '[PRODUCT]': replacement})

    def test_stock_verification_retains_replacements_with_their_items(self):
        request = item.normalize_request(None, [product(replacement_product='Replacement Tee'), product(
            style='5000', description='Cotton Tee', color='Black', total_quantity=3,
            design_item_ids=['design-item-102'], replacement_product='Different Tee')])
        verified = item.verify_selected_stock(snapshot(), request['products'])
        self.assertEqual([(row['style'], row['replacement_product']) for row in verified],
                         [('DM130', 'Replacement Tee'), ('5000', 'Different Tee')])

    def test_existing_multi_item_requests_retain_their_original_global_text(self):
        request = item.normalize_request('Original combined replacement text', [product(), product(
            style='5000', description='Cotton Tee', color='Black', total_quantity=3,
            design_item_ids=['design-item-102'])])
        self.assertEqual(item.placeholder_values('1234567', request)['[PRODUCT]'], 'Original combined replacement text')
        self.assertTrue(all('replacement_product' not in row for row in request['products']))

    def test_stock_comes_from_saved_selected_items_and_excludes_other_stock(self):
        products = item.verify_selected_stock(snapshot(), self.request()["products"])
        values = item.placeholder_values("1234567", {**self.request(), "products": products})
        self.assertEqual(values, {"[ORDER-NUMBER]": "1234567",
                                 "[STOCK]": "DM130 red District Perfect Tri Tee",
                                 "[PRODUCT]": "5000 Black Gildan Cotton Tee"})
        self.assertNotIn("Cotton Tee", values["[STOCK]"])

    def test_changed_missing_and_ambiguous_stock_stops(self):
        for mutation in ("style", "quantity", "missing", "duplicate"):
            current = snapshot()
            original = current["designs"][0]["items"][0]
            if mutation == "style":
                original["style"] = "5000"
            elif mutation == "quantity":
                original["sizes"][0]["quantity"] = 3
            elif mutation == "missing":
                current["designs"][0]["items"].pop(0)
            else:
                current["designs"][0]["items"].append(copy.deepcopy(original))
            with self.subTest(mutation=mutation), self.assertRaises(item.StockIssueItemError):
                item.verify_selected_stock(current, self.request()["products"])

    def test_merged_product_rows_across_tabs_verify_their_total(self):
        current = snapshot()
        second = copy.deepcopy(current["designs"][0]["items"][0])
        second["id"] = "201"
        current["designs"].append({"items": [second]})
        products = item.normalize_request("Replacement", [product(
            design_item_ids=["design-item-101", "design-item-201"], total_quantity=8)])["products"]
        verified = item.verify_selected_stock(current, products)
        self.assertEqual(verified[0]["total_quantity"], 8)
        self.assertEqual(verified[0]["tab_numbers"], [1, 2])

    def test_missing_template_tokens_stop_before_opening_salesforce(self):
        valid = {"subject": "Order #[ORDER-NUMBER]", "body_html": "<p>[STOCK] [PRODUCT]</p>"}
        for token in ("[ORDER-NUMBER]", "[STOCK]", "[PRODUCT]"):
            template = {key: value.replace(token, "") for key, value in valid.items()}
            store = mock.Mock()
            store.get_template.return_value = template
            with self.subTest(token=token), mock.patch.object(item, "default_template_store", return_value=store), \
                    mock.patch.object(item.shared, "_open_salesforce_account") as open_account:
                with self.assertRaisesRegex(item.StockIssueItemError, "Keep"):
                    item._prepare_email(mock.Mock(), "crm", "1234567", "customer@example.test", self.request())
            open_account.assert_not_called()

    def test_changed_stock_in_worker_never_prepares_an_email(self):
        with ExitStack() as stack:
            for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "safe_driver_quit", "safe_take_screenshot"):
                stack.enter_context(mock.patch.object(item.shared, name))
            stack.enter_context(mock.patch.object(item.shared, "_open_driver", return_value=mock.Mock()))
            current = snapshot()
            current["designs"][0]["items"][0]["color"] = "Blue"
            stack.enter_context(mock.patch.object(item.stock_reader, "_read_snapshot", return_value=current))
            send = stack.enter_context(mock.patch.object(item, "_send_email"))
            ok, message, payload = item.run_stock_issue_item_order("1234567", "Replacement", [product()])
        self.assertFalse(ok)
        self.assertIn("stock changed", message)
        self.assertEqual(payload["failed_stage"], "stock_verification")
        send.assert_not_called()


class StockIssueItemReceiptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patch = mock.patch.object(item, "STATE_DIR", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.request = item.normalize_request("Replacement Tee", [product()])
        self.draft = {"subject": "Order #1234567 Stock", "body": "Original and replacement", "sent": False}

    def send(self, **kwargs):
        return item._send_email(mock.Mock(), "crm", "1234567", "customer@example.test", self.request, **kwargs)

    def test_confirmed_receipt_skips_drafting_and_send_even_after_title_edit(self):
        with mock.patch.object(item, "_prepare_email", return_value=self.draft) as prepare, \
                mock.patch.object(item.confirmation, "send_and_confirm", return_value={"activity_verified": True}) as send:
            first = self.send()
            prepare.return_value = {**self.draft, "subject": "Edited title"}
            second = self.send()
        self.assertTrue(first["sent"])
        self.assertTrue(second["skipped"])
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["receipt_scope"], item.AUTOMATION_KEY)
        self.assertTrue(callable(send.call_args.kwargs["prepare_retry"]))

    def test_receipt_binds_replacements_to_stock_and_allows_reordered_matching_pairs(self):
        self.request = item.normalize_request(None, [product(replacement_product='Replacement Tee'), product(
            style='5000', description='Cotton Tee', color='Black', total_quantity=3,
            design_item_ids=['design-item-102'], replacement_product='Different Tee')])
        with mock.patch.object(item, '_prepare_email', return_value=self.draft) as prepare, \
                mock.patch.object(item.confirmation, 'send_and_confirm', return_value={'activity_verified': True}) as send:
            self.send()
            self.request['products'].reverse()
            self.assertTrue(self.send()['skipped'])
            rows = self.request['products']
            rows[0]['replacement_product'], rows[1]['replacement_product'] = rows[1]['replacement_product'], rows[0]['replacement_product']
            with self.assertRaisesRegex(item.StockIssueItemError, 'receipt cannot be reused'):
                self.send()
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(send.call_count, 1)

    def test_per_item_request_recognizes_a_confirmed_legacy_single_item_email(self):
        with mock.patch.object(item, '_prepare_email', return_value=self.draft) as prepare, \
                mock.patch.object(item.confirmation, 'send_and_confirm', return_value={'activity_verified': True}) as send:
            self.send()
            self.request = item.normalize_request(None, [product(replacement_product='Replacement Tee')])
            self.assertTrue(self.send()['skipped'])
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(send.call_count, 1)

    def test_old_multi_item_receipt_is_not_reinterpreted_as_new_individual_pairings(self):
        second = product(style='5000', description='Cotton Tee', color='Black', total_quantity=3,
                         design_item_ids=['design-item-102'])
        self.request = item.normalize_request('Replacement Tee and Different Tee', [product(), second])
        with mock.patch.object(item, '_prepare_email', return_value=self.draft) as prepare, \
                mock.patch.object(item.confirmation, 'send_and_confirm', return_value={'activity_verified': True}) as send:
            self.send()
            self.request = item.normalize_request(None, [product(replacement_product='Replacement Tee'),
                                                        {**second, 'replacement_product': 'Different Tee'}])
            with self.assertRaisesRegex(item.StockIssueItemError, 'receipt cannot be reused'):
                self.send()
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(send.call_count, 1)

    def test_per_item_request_cannot_restart_a_legacy_uncertain_send_with_changed_copy(self):
        with mock.patch.object(item, '_prepare_email', return_value=self.draft) as prepare, \
                mock.patch.object(item.confirmation, 'send_and_confirm', side_effect=RuntimeError('Unconfirmed')) as send:
            with self.assertRaisesRegex(RuntimeError, 'Unconfirmed'):
                self.send()
            self.request = item.normalize_request(None, [product(replacement_product='Replacement Tee')])
            prepare.return_value = {**self.draft, 'subject': 'Edited title'}
            with self.assertRaisesRegex(item.StockIssueItemError, 'draft changed'):
                self.send()
        self.assertEqual(send.call_count, 1)

    def test_uncertain_send_cannot_restart_with_changed_draft_or_replacement(self):
        with mock.patch.object(item, "_prepare_email", return_value=self.draft) as prepare, \
                mock.patch.object(item.confirmation, "send_and_confirm", side_effect=RuntimeError("Unconfirmed")) as send:
            with self.assertRaisesRegex(RuntimeError, "Unconfirmed"):
                self.send()
            prepare.return_value = {**self.draft, "subject": "Edited title"}
            with self.assertRaisesRegex(item.StockIssueItemError, "draft changed"):
                self.send()
            self.request["product"] = "Different replacement"
            with self.assertRaisesRegex(item.StockIssueItemError, "receipt cannot be reused"):
                self.send()
            self.assertEqual(send.call_count, 1)

    def test_dry_run_never_reserves_or_sends(self):
        with mock.patch.object(item, "_prepare_email", return_value=self.draft), \
                mock.patch.object(item.confirmation, "send_and_confirm") as send:
            result = self.send(dry_run=True)
        self.assertFalse(result["sent"])
        self.assertFalse((self.root / "stock_issue_item_emails").exists())
        send.assert_not_called()

    def test_missing_activity_confirmation_never_marks_a_receipt_confirmed(self):
        with mock.patch.object(item, "_prepare_email", return_value=self.draft), \
                mock.patch.object(item.confirmation, "send_and_confirm", return_value={"activity_verified": False}):
            with self.assertRaisesRegex(item.StockIssueItemError, "Activity has not confirmed"):
                self.send()
        receipt = json.loads((self.root / "stock_issue_item_emails" / "1234567.json").read_text())
        self.assertFalse(receipt["confirmed"])


class StockIssueItemQueueTests(unittest.TestCase):
    ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"

    def test_bridge_requires_input_and_preserves_it_for_queue_restore(self):
        server.app.config.update(TESTING=True)
        client = server.app.test_client()
        payload = {"order_id": "1234567", "automation": "stock_issue_item", "products": [product()]}
        with mock.patch.object(server, "enqueue_automation", return_value=(True, "Queued", {"id": "item-1"})) as enqueue:
            missing = client.post("/api/extension/bridge/process-order/manual", json=payload,
                                  headers={"Origin": self.ORIGIN}, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(missing.status_code, 409)
            enqueue.assert_not_called()
            response = client.post("/api/extension/bridge/process-order/manual",
                                   json={**payload, "product": " Replacement, Navy "}, headers={"Origin": self.ORIGIN},
                                   environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(response.status_code, 202)
        arguments = enqueue.call_args.kwargs
        self.assertEqual(arguments["task_type"], "crm.stock_issue_item")
        self.assertEqual(arguments["task_arguments"]["product"], "Replacement, Navy")
        self.assertIn(arguments["task_type"], server._RETRYABLE_SINGLE_ORDER_TASK_TYPES)
        runner, _ = server._restore_automation_queue_callables(arguments)
        self.assertEqual(runner.keywords["request_data"]["product"], "Replacement, Navy")
        self.assertEqual(runner.keywords["request_data"]["products"][0]["design_item_ids"], ["design-item-101"])

    def test_bridge_preserves_individual_replacements_for_queue_restore_and_worker(self):
        server.app.config.update(TESTING=True)
        client = server.app.test_client()
        products = [product(replacement_product='Replacement Tee'), product(
            style='5000', description='Cotton Tee', color='Black', total_quantity=3,
            design_item_ids=['design-item-102'], replacement_product=' Different Tee ')]
        payload = {'order_id': '1234567', 'automation': 'stock_issue_item', 'products': products}
        with mock.patch.object(server, 'enqueue_automation', return_value=(True, 'Queued', {'id': 'item-pairs'})) as enqueue:
            response = client.post('/api/extension/bridge/process-order/manual', json=payload,
                headers={'Origin': self.ORIGIN}, environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(response.status_code, 202)
        arguments = enqueue.call_args.kwargs
        self.assertIsNone(arguments['task_arguments']['product'])
        runner, _ = server._restore_automation_queue_callables(arguments)
        saved_products = runner.keywords['request_data']['products']
        self.assertEqual([(row['style'], row['replacement_product']) for row in saved_products],
                         [('DM130', 'Replacement Tee'), ('5000', 'Different Tee')])
        with mock.patch.object(server, 'crm_lock') as lock, \
                mock.patch.object(server, '_run_script', return_value=(True, 'Sent', {})) as run:
            lock.acquire.return_value = True
            server.run_crm_stock_issue_item_queued('1234567', None, saved_products)
        worker_request = json.loads(run.call_args.args[1][3])
        self.assertNotIn('product', worker_request)
        self.assertEqual(worker_request['products'], saved_products)

    def test_bridge_rejects_partial_pairings_even_with_a_global_fallback(self):
        server.app.config.update(TESTING=True)
        client = server.app.test_client()
        with mock.patch.object(server, 'enqueue_automation') as enqueue:
            response = client.post('/api/extension/bridge/process-order/manual', json={
                'order_id': '1234567', 'automation': 'stock_issue_item', 'product': 'Do not use globally',
                'products': [product(replacement_product='Valid'), product(style='5000',
                    design_item_ids=['design-item-102'])]}, headers={'Origin': self.ORIGIN},
                environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(response.status_code, 409)
        enqueue.assert_not_called()

    def test_worker_runs_as_a_single_order_subprocess_under_the_crm_lock(self):
        with mock.patch.object(server, "crm_lock") as lock, \
                mock.patch.object(server, "_run_script", return_value=(True, "Sent", {})) as run:
            lock.acquire.return_value = True
            server.run_crm_stock_issue_item_queued("1234567", "Replacement", [product()])
        lock.acquire.assert_called_once_with(blocking=False)
        lock.release.assert_called_once()
        self.assertEqual(run.call_args.args[0], server.CRM_STOCK_ISSUE_ITEM_SCRIPT)
        args = run.call_args.args[1]
        self.assertEqual(args[:2], ["--order-id", "1234567"])
        self.assertEqual(json.loads(args[3])["product"], "Replacement")
        self.assertIn("--real", args)
        self.assertTrue(run.call_args.kwargs["require_result"])
        self.assertIn(Path(server.CRM_STOCK_ISSUE_ITEM_SCRIPT).name, server.CRM_VISIBLE_FLAG_WORKER_FILENAMES)


if __name__ == "__main__":
    unittest.main()
