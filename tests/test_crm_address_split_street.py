import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKERS_DIR = ROOT / "workers"
for path in (ROOT, WORKERS_DIR):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

import crm_validate_address  # noqa: E402


class SplitStreetFallbackTests(unittest.TestCase):
    def test_existing_address_is_rewritten_before_it_is_persisted(self):
        split = {
            "recipient": "Stephen Heller",
            "address": "12796",
            "address_cont": "Cliffshore Drive",
            "city": "Lake Country",
            "state": "British Columbia",
            "zip": "V4V 2P7",
        }
        merged = {**split, "address": "12796 Cliffshore Drive", "address_cont": ""}
        best_existing = {
            "option": {
                "text": "Stephen Heller - 12796 Cliffshore Drive Lake Country BC, V4V 2P7",
                "preferred_all_caps": False,
            },
            "assessment": {"city_only_mismatch": False},
        }
        events = []

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text", return_value=True))
            stack.enter_context(mock.patch.object(
                crm_validate_address,
                "_ensure_recipient_present",
                side_effect=[(True, dict(split)), (True, dict(merged))],
            ))
            stack.enter_context(mock.patch.object(
                crm_validate_address,
                "_rewrite_address_fields_if_needed",
                side_effect=lambda *_args, **_kwargs: (events.append("rewrite") or (dict(merged), "")),
            ))
            stack.enter_context(mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_final_save_ready", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_current_address", return_value=dict(merged)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False))
            stack.enter_context(mock.patch.object(
                crm_validate_address,
                "_persist_validated_address_via_modal_scope",
                side_effect=lambda *_args, **_kwargs: (events.append("persist") or {"ok": True}),
            ))
            stack.enter_context(mock.patch.object(
                crm_validate_address,
                "_prepare_shipping_form_for_save",
                return_value=(None, dict(merged), ""),
            ))
            stack.enter_context(mock.patch.object(crm_validate_address, "_save_shipping_transaction"))
            result = crm_validate_address._try_resolve_with_existing_address(
                object(), object(), "4845117", False, dict(split), dict(merged), "", [],
                existing_options=[best_existing["option"]],
                best_existing=best_existing,
                accept_save_button_ready=False,
                allow_assessed_current_address=True,
                allow_rewrite=True,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["final_address"]["address"], "12796 Cliffshore Drive")
        self.assertEqual(result["final_address"]["address_cont"], "")
        self.assertEqual(events, ["rewrite", "persist"])


if __name__ == "__main__":
    unittest.main()
