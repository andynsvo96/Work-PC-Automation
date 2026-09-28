import json
import shutil
import subprocess
import sys
import unittest
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
        with mock.patch.object(separator, "_order_scope", return_value=True) as scope:
            self.assertTrue(separator._order_has_screenprinting(mock.Mock()))
        script = scope.call_args.args[1]
        program = "const check = new Function('s', 'r', " + json.dumps(script) + ");\n" + r"""
        const assert = require('assert');
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
        """
        subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)

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
