import json
import subprocess
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from workers import crm_sleeve_prints as extra


NOTE = "Additional embroidery area\n$9.00 each\nemailed txted"
MUTATION = {
    "existing_areas": [{"tab_number": 1, "description": "Front-Left Chest", "method": "Embroidery"}],
    "price_updates": [{"tab_number": 1, "item_index": 0, "size_index": None, "price": "59.05"}],
}


class ExtraPrintVerificationTests(unittest.TestCase):
    def scope(self, _driver, script, *args):
        harness = """
const {script, resource, args} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const result = new Function('s', 'r', 'return function(){' + script + '}')({}, resource)(...args);
process.stdout.write(JSON.stringify(result));
"""
        return json.loads(subprocess.check_output(
            ["node", "-e", harness], text=True,
            input=json.dumps({"script": script, "resource": self.resource, "args": args}),
        ))

    def setUp(self):
        self.resource = {
            "salesNotes": "Earlier note", "addSalesNotes": NOTE,
            "designs": [{"printAreas": [{"description": "Front-Left Chest", "methodDescription": "Embroidery"}],
                         "designItems": [{"splitIntoSizes": 0, "pricePerPiece": "59.05"}]}],
        }
        self.scope_patch = patch.object(extra.shared, "_order_scope", side_effect=self.scope)
        self.scope_patch.start()
        self.addCleanup(self.scope_patch.stop)

    def test_draft_is_not_a_persisted_note_in_state_or_verification(self):
        self.assertFalse(extra._crm_note_exists(extra._read_crm_sleeve_state(None), NOTE))
        verification = extra._verify_crm_sleeve_changes(None, NOTE, MUTATION, require_note=False)
        self.assertFalse(verification["note_saved"])

    def test_saved_note_matches_crlf_flattened_whitespace_and_filtered_notes(self):
        for field in ("salesNotes", "filteredSalesNotes"):
            for separator in ("\r\n", "  "):
                with self.subTest(field=field, separator=separator):
                    self.resource[field] = "Earlier note\n" + NOTE.replace("\n", separator).upper()
                    self.assertTrue(extra._crm_note_exists(extra._read_crm_sleeve_state(None), NOTE))
                    self.assertTrue(extra._verify_crm_sleeve_changes(None, NOTE, MUTATION)["note_saved"])
                    self.resource.pop(field)

    def test_missing_areas_or_wrong_prices_stop_even_when_note_is_missing(self):
        for missing_area in (False, True):
            with self.subTest(missing_area=missing_area):
                self.resource["designs"][0]["designItems"][0]["pricePerPiece"] = "50.05"
                if missing_area:
                    self.resource["designs"][0]["printAreas"] = []
                message = "Extra Print Areas were not confirmed" if missing_area else "prices were not confirmed"
                with self.assertRaisesRegex(extra.SleevePrintsError, message) as raised:
                    extra._verify_crm_sleeve_changes(None, NOTE, MUTATION, require_note=False)
                self.assertNotIn("CRM saved the order", str(raised.exception))


class ExtraPrintNoteRecoveryTests(unittest.TestCase):
    def pipeline(self, stack, verification):
        for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed",
                     "_switch_to_crm_app_frame", "_wait_for_order_scope", "_order_scope"):
            stack.enter_context(patch.object(extra.shared, name))
        enter = stack.enter_context(patch.object(extra.shared._product_separator, "_enter_edit_mode"))
        save = stack.enter_context(patch.object(extra.shared, "_save_order_and_wait", return_value={"editMode": False}))
        verify = stack.enter_context(patch.object(extra, "_verify_crm_sleeve_changes", side_effect=verification))
        apply = stack.enter_context(patch.object(extra, "_apply_crm_sleeve_changes"))
        return enter, save, verify, apply

    def run_recovery(self, **kwargs):
        return extra._verify_saved_crm_sleeve_changes(
            Mock(), "https://crm.test/order/1234567", "1234567", NOTE, MUTATION, **kwargs,
        )

    def test_note_only_save_is_bounded_and_rechecks_all_targets_after_reload(self):
        with ExitStack() as stack:
            enter, save, verify, apply = self.pipeline(stack, [{"note_saved": False}, {"note_saved": True}])
            result = self.run_recovery()
            self.assertTrue(result["note_repaired"])
            enter.assert_called_once()
            save.assert_called_once()
            apply.assert_not_called()
            self.assertEqual(extra.shared.safe_get_with_partial_load.call_count, 2)
            self.assertEqual([call.args[2] for call in verify.call_args_list], [MUTATION, MUTATION])
            self.assertEqual(verify.call_args_list[0].kwargs, {"require_note": False})
            self.assertEqual(verify.call_args_list[1].kwargs, {})

    def test_existing_note_and_dry_run_skip_note_writes(self):
        for present in (False, True):
            with self.subTest(present=present), ExitStack() as stack:
                enter, save, _, apply = self.pipeline(stack, [{"note_saved": present}])
                if present:
                    self.assertTrue(self.run_recovery(dry_run=True)["note_saved"])
                else:
                    with self.assertRaisesRegex(extra.SleevePrintsError, "dry-run mode skipped"):
                        self.run_recovery(dry_run=True)
                enter.assert_not_called()
                save.assert_not_called()
                apply.assert_not_called()

    def test_changed_areas_or_prices_stop_before_note_save(self):
        with ExitStack() as stack:
            enter, save, _, apply = self.pipeline(stack, extra.SleevePrintsError("prices were not confirmed"))
            with self.assertRaisesRegex(extra.SleevePrintsError, "prices were not confirmed"):
                self.run_recovery()
            enter.assert_not_called()
            save.assert_not_called()
            apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
