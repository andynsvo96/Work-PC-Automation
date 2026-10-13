import json
import shutil
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_product_separator as separator
import server


class SeparatorReviewTests(unittest.TestCase):
    def test_warning_survives_batch_and_history_normalization(self):
        payload = {"order_results": [{
            "order_id": "1234567", "success": True, "resolution": "split_complete",
            "report": {"screenprinting_warning": True},
        }, {"order_id": "1234568", "success": True, "resolution": "skipped_no_split_needed"}]}
        rows = server._build_crm_product_separator_order_results(payload)
        rows = server._build_crm_product_separator_order_results({"order_results": rows})
        self.assertTrue(rows[0]["screenprinting_warning"])
        self.assertFalse(rows[1]["screenprinting_warning"])
        steps = server._normalize_crm_processing_step_results([{
            "key": "product_separator", "success": True,
            "screenprinting_orders": [row for row in rows if row["screenprinting_warning"]],
        }])
        self.assertEqual([o["order_id"] for o in steps[0]["screenprinting_orders"]], ["1234567"])

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser checks")
    def test_screenprinting_detection_uses_active_print_methods(self):
        plan = {"split_tabs": [{"source_tab_number": 1}]}
        with mock.patch.object(separator, "_order_scope", return_value=True) as scope:
            self.assertTrue(separator._split_tabs_have_screenprinting(mock.Mock(), plan))
        self.assertEqual(scope.call_args.args[2], [1])
        script = scope.call_args.args[1]
        program = (
            "const makeCheck = new Function('s', 'r', "
            + json.dumps("return function () {\n" + script + "\n};") + ");\n"
        ) + r"""
        const assert = require('assert');
        const check = (s, r, tabs = [1]) => makeCheck(s, r)(tabs);
        const order = (area, extra = {}) => ({designs: [{printAreas: [area], ...extra}]});
        for (const name of ['Screen Printing', 'screenprinting', 'Screen Print', 'SCREEN-PRINTING']) {
          assert.strictEqual(check({}, order({printMethodDescription: name})), true);
        }
        assert.strictEqual(check({}, order({printMethod: {description: 'Screen Printing'}})), true);
        assert.strictEqual(check({}, order({printMethodDescription: 'HD Digital'})), false);
        assert.strictEqual(check({}, order({printMethodDescription: 'Screen Printing', crudAction: 'd'})), false);
        assert.strictEqual(check({}, order({printMethodDescription: 'Screen Printing'}, {crudAction: 'd'})), false);
        assert.strictEqual(check({$parent: {setPrintMethodDescription: () => 'Screen Printing'}}, order({printMethodId: 2})), true);
        assert.strictEqual(check({}, order({})), false);
        const mixed = {designs: [
          {printAreas: [{printMethodDescription: 'Screen Printing'}]},
          {printAreas: [{printMethodDescription: 'HD Digital'}]},
          {printAreas: [{printMethodDescription: 'HD Digital'}]},
          {printAreas: [{printMethodDescription: 'Screen Printing'}]},
        ]};
        assert.strictEqual(check({}, mixed, [3]), false);
        assert.strictEqual(check({}, mixed, [2, 3]), false);
        assert.strictEqual(check({}, mixed, [1, 3]), true);
        assert.strictEqual(check({}, mixed, []), false);
        mixed.designs[0].crudAction = 'd';
        mixed.designs[2].printAreas[0].printMethodDescription = 'Screen Printing';
        assert.strictEqual(check({}, mixed, [3]), true);
        assert.strictEqual(check({}, mixed, [2]), false);
        """
        subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)

    def test_screenprinting_detection_skips_empty_split_plan(self):
        with mock.patch.object(separator, "_order_scope") as scope:
            self.assertFalse(separator._split_tabs_have_screenprinting(mock.Mock(), {"split_tabs": []}))
        scope.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser checks")
    def test_live_separator_warns_only_for_source_tabs_before_separation(self):
        adult = separator._classify_product({"product_name": "Adult Tee"})
        youth = separator._classify_product({"product_name": "Youth Tee"})
        scan = {"tabs": [
            {"tab_number": 1, "tab_name": "H-Test001", "quantity": 10,
             "needs_split": False, "products": [adult]},
            {"tab_number": 2, "tab_name": "H-Test002", "quantity": 2,
             "needs_split": True, "products": [adult, youth], "custom_names_and_numbers_present": True},
            {"tab_number": 3, "tab_name": "H-Test003", "quantity": 2,
             "needs_split": True, "products": [adult, youth]},
        ]}
        plan = separator._build_separator_plan(scan)
        self.assertEqual([tab["source_tab_number"] for tab in plan["split_tabs"]], [3])
        clean_scan = {"tabs": [
            {"tab_number": 1, "quantity": 10}, {"tab_number": 2, "quantity": 2},
            {"tab_number": 3, "quantity": 1}, {"tab_number": 4, "quantity": 1},
        ]}

        for method, expected in (("HD Digital", False), ("Embroidery", False), ("Screen Printing", True)):
            with self.subTest(source_method=method):
                resource = {"designs": [
                    {"printAreas": [{"printMethodDescription": "Screen Printing"}]},
                    {"printAreas": [{"printMethodDescription": "Screen Printing"}]},
                    {"printAreas": [{"printMethodDescription": method}]},
                ]}

                def execute_script(script, *args):
                    program = (
                        "const r = " + json.dumps(resource) + ";\n"
                        "const s = {order: {getResource: () => r}, copyOrder() {}};\n"
                        "const document = {querySelectorAll: () => [{}]};\n"
                        "const angular = {element: () => ({scope: () => s})};\n"
                        "console.log(JSON.stringify(new Function('document', 'angular', "
                        + json.dumps("return function () {\n" + script + "\n};")
                        + ")(document, angular)(..." + json.dumps(args) + ")));"
                    )
                    result = subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)
                    return json.loads(result.stdout)

                driver = mock.Mock()
                driver.execute_script.side_effect = execute_script

                def apply_split(*args):
                    resource["designs"] = []
                    return {"saved": True}

                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(separator, "_build_driver", return_value=(driver, None)))
                    for name in ("safe_get_with_partial_load", "_handle_login_if_needed", "_wait_for_crm_context",
                                 "_recover_not_authenticated_page", "safe_driver_quit"):
                        stack.enter_context(mock.patch.object(separator, name))
                    stack.enter_context(mock.patch.object(separator, "_scan_order", return_value=scan))
                    apply = stack.enter_context(mock.patch.object(separator, "_apply_live_split", side_effect=apply_split))
                    stack.enter_context(mock.patch.object(separator, "_verify_split_persisted_after_save",
                                                         return_value=({"scan_after_refresh": clean_scan}, [])))
                    write = stack.enter_context(mock.patch.object(separator, "_write_result"))
                    exit_code = separator.run_product_separator_order(order_id="5416479", dry_run=False)

                self.assertEqual(exit_code, 0)
                apply.assert_called_once()
                self.assertEqual(driver.execute_script.call_args.args[1], [3])
                payload = write.call_args.kwargs
                self.assertEqual(payload["resolution"], "split_complete")
                self.assertIs(payload["report"]["screenprinting_warning"], expected)
                rows = server._build_crm_product_separator_order_results({
                    "order_results": [{"order_id": "5416479", "success": True, **payload}],
                })
                self.assertIs(rows[0]["screenprinting_warning"], expected)

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser checks")
    def test_combined_review_and_acknowledgment(self):
        html = (ROOT / "ui_panel.html").read_text(encoding="utf-8")
        functions = html[html.index("function crmAutoSplitReviewSignature("):html.index("function crmProcessingRunTotals(")]
        program = functions + r"""
        const assert = require('assert');
        const split = {key: 'auto_splitter', split_orders: [{order_id: '1234567'}]};
        const separate = {key: 'product_separator', screenprinting_orders: [{order_id: '1234568'}]};
        const payload = steps => ({state: {run_history: [{timestamp: '2026-09-28T12:00:00', step_results: steps}]}});
        assert.strictEqual(crmAutoSplitReviewRuns(payload([])).length, 0);
        assert.strictEqual(crmAutoSplitReviewRuns(payload([separate]))[0].sections[0].title, 'Auto Separator');
        const old = crmAutoSplitReviewRuns(payload([split]))[0];
        assert.strictEqual(old.signature, crmAutoSplitReviewSignature(old.timestamp, old.orders));
        const combined = crmAutoSplitReviewRuns(payload([separate, split]));
        assert.strictEqual(combined.length, 1);
        assert.deepStrictEqual(combined[0].sections.map(s => s.title), ['Auto Separator', 'Auto Splitter']);
        function element() {
          const classes = new Set();
          return {children: [], dataset: {}, classList: {
            contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c)
          }, appendChild(child) {this.children.push(child)}, setAttribute() {}, focus() {}};
        }
        const elements = {};
        const document = {getElementById: id => elements[id] ||= element(), createElement: element};
        const storage = new Map();
        const localStorage = {getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v)};
        const crmAutoSplitReviewAcknowledged = new Set();
        const CRM_AUTO_SPLIT_REVIEW_STORAGE_PREFIX = 'test:';
        const fmtDateTime = value => value;
        const setTimeout = fn => fn();
        const crmProcessingStatusPayload = payload([separate, split]);
        maybeShowCrmAutoSplitReview(crmProcessingStatusPayload);
        const modal = elements.crmAutoSplitReviewModal;
        assert.strictEqual(modal.classList.contains('active'), true);
        assert.deepStrictEqual(elements.crmAutoSplitReviewList.children.filter(e => e.textContent?.startsWith('Auto ')).map(e => e.textContent),
          ['Auto Separator', 'Auto Splitter', 'Auto Splitter created the following CRM orders. Review every created order.']);
        acknowledgeCrmAutoSplitReview();
        assert.strictEqual(modal.classList.contains('active'), false);
        crmAutoSplitReviewAcknowledged.clear();
        maybeShowCrmAutoSplitReview(crmProcessingStatusPayload);
        assert.strictEqual(modal.classList.contains('active'), false);
        """
        subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
