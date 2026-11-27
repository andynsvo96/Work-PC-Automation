import ast
import re
import unittest
from pathlib import Path

import config_defaults
from crm_list_url import CRM_LINK_SETTING_KEYS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigContractTests(unittest.TestCase):
    def test_every_grouped_crm_link_has_an_editable_config_default(self):
        html = (PROJECT_ROOT / "ui_panel.html").read_text(encoding="utf-8")
        groups = re.search(r"const CRM_LINK_SETTING_GROUPS = \[([\s\S]+?)\n\];", html).group(1)
        contracted = re.search(r"const CRM_RUSH_CONTRACTED_LINK_KEYS = \{([\s\S]+?)\n\};", html).group(1)
        keys = re.findall(r"'([A-Z][A-Z0-9_]+)'", groups + contracted)
        self.assertCountEqual(keys, CRM_LINK_SETTING_KEYS)
        self.assertTrue(all(isinstance(getattr(config_defaults, key), str) for key in keys))

    def test_every_direct_config_import_exists_in_tracked_defaults(self):
        missing = []
        source_roots = [PROJECT_ROOT, PROJECT_ROOT / "workers", PROJECT_ROOT / "routes"]
        files = []
        for source_root in source_roots:
            files.extend(source_root.glob("*.py"))

        for source_path in sorted(set(files)):
            tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != "config":
                    continue
                for imported in node.names:
                    if imported.name != "*" and not hasattr(config_defaults, imported.name):
                        missing.append(f"{source_path.name}:{imported.name}")

        self.assertEqual(missing, [], f"Settings missing from config_defaults.py: {missing}")

    def test_example_config_inherits_the_tracked_defaults(self):
        tree = ast.parse((PROJECT_ROOT / "config.example.py").read_text(encoding="utf-8-sig"))
        imports_defaults = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "config_defaults"
            and any(name.name == "*" for name in node.names)
            for node in tree.body
        )
        self.assertTrue(imports_defaults)

    def test_paycom_hours_default_targets_the_read_only_timecard_view(self):
        self.assertTrue(
            config_defaults.PAYCOM_HOURS_URL.endswith("/timecard/WEB02#!timecard-view")
        )


if __name__ == "__main__":
    unittest.main()
