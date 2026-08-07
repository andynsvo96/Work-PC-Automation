import re
import unittest
from pathlib import Path


class SettingsUiStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1] / "ui_panel.html").read_text(encoding="utf-8")

    def test_settings_categories_are_present(self):
        expected = {"overview", "connections", "crm", "work", "slack", "system", "advanced"}
        sections = set(re.findall(r"<section[^>]+data-settings-section=['\"]([^'\"]+)", self.html))
        targets = set(re.findall(r"<button[^>]+data-settings-target=['\"]([^'\"]+)", self.html))
        self.assertEqual(sections, expected)
        self.assertEqual(targets, expected)

    def test_dynamic_settings_roots_remain_unique(self):
        required_ids = {
            "settingsStatusBox",
            "crmPreferenceFields",
            "crmFields",
            "workFields",
            "paycomFields",
            "slackFields",
            "otherFields",
            "nodeHardwareSummary",
            "workerModeSelect",
            "manualWorkerCountInput",
            "workerRecommendationText",
        }
        for element_id in required_ids:
            with self.subTest(element_id=element_id):
                self.assertEqual(
                    len(re.findall(rf"\bid=['\"]{re.escape(element_id)}['\"]", self.html)),
                    1,
                )

    def test_settings_interactions_are_wired(self):
        for function_name in (
            "showSettingsSection",
            "filterSettingsNavigation",
            "updateSettingsOverview",
            "setSettingsSaveState",
            "reloadSettingsPage",
        ):
            with self.subTest(function_name=function_name):
                self.assertIn(f"function {function_name}(", self.html)

    def test_connection_cards_use_container_responsive_columns(self):
        self.assertRegex(
            self.html,
            r"\.settings-connection-grid\{[^}]*grid-template-columns:repeat\(auto-fit,minmax\(140px,1fr\)\)",
        )


if __name__ == "__main__":
    unittest.main()
