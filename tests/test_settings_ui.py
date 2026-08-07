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

    def test_primary_navigation_is_consolidated(self):
        self.assertIn("id='tabBtnAutomation'", self.html)
        self.assertIn("id='tabBtnProcessing'", self.html)
        self.assertIn("id='tabBtnSystem'", self.html)
        self.assertNotIn("id='tabBtnMetrics'", self.html)
        self.assertNotIn("id='tabBtnPower'", self.html)
        self.assertIn("id='tabSystem'", self.html)

    def test_processing_sections_and_main_run_views_are_present(self):
        expected = {"run", "reports", "tools"}
        sections = set(re.findall(r"data-processing-section=['\"]([^'\"]+)", self.html))
        targets = set(re.findall(r"data-processing-target=['\"]([^'\"]+)", self.html))
        self.assertEqual(sections, expected)
        self.assertEqual(targets, expected)
        for element_id in (
            "crmProcessingLatestRunId",
            "crmProcessingLatestState",
            "crmProcessingLatestWhen",
            "crmProcessingLatestMode",
            "crmProcessingLatestOrders",
            "crmProcessingLatestErrors",
            "crmProcessingLatestDuration",
            "crmProcessingLatestSteps",
            "crmProcessingMainHistoryRows",
            "crmProcessingToolHealthRows",
        ):
            self.assertEqual(len(re.findall(rf"\bid=['\"]{element_id}['\"]", self.html)), 1)

    def test_sheet_scanner_has_one_simple_live_action(self):
        self.assertEqual(len(re.findall(r"id=['\"]crmMassEmailerRunBtn['\"]", self.html)), 1)
        for removed_id in (
            "crmMassEmailerScanBtn",
            "crmMassEmailerDryRunBtn",
            "crmMassEmailerLimitInput",
            "crmMassEmailerRetryErrorsInput",
        ):
            self.assertNotRegex(self.html, rf"\bid=['\"]{removed_id}['\"]")

    def test_system_sections_are_merged_and_wired(self):
        expected = {"overview", "hardware", "clipboard", "power"}
        sections = set(re.findall(r"data-system-section=['\"]([^'\"]+)", self.html))
        targets = set(re.findall(r"data-system-target=['\"]([^'\"]+)", self.html))
        self.assertEqual(sections, expected)
        self.assertEqual(targets, expected)
        self.assertIn("function showSystemSection(", self.html)
        self.assertIn("function showProcessingSection(", self.html)
        self.assertIn("function refreshSystemOverview(", self.html)
        self.assertIn("function scheduleOverviewPowerAction(", self.html)

    def test_approved_dashboard_sections_are_wired_to_live_state(self):
        for function_name in (
            "updateCommunicationDashboard",
            "renderCrmProcessingMainRunViews",
            "renderDesktopMetrics",
            "updateSystemActivityOverview",
        ):
            with self.subTest(function_name=function_name):
                self.assertIn(f"function {function_name}(", self.html)

    def test_compact_layout_has_narrow_resolution_rules(self):
        self.assertRegex(self.html, r"@media \(max-width:560px\)\{")
        self.assertRegex(
            self.html,
            r"@media \(max-width:560px\)\{.*?\.tab-bar\{[^}]*grid-template-columns:repeat\(3,minmax\(0,1fr\)\)",
        )
        self.assertRegex(
            self.html,
            r"@media \(max-width:560px\)\{.*?\.rough-system-metrics\{grid-template-columns:1fr\}",
        )

    def test_existing_backend_control_roots_remain_unique(self):
        for element_id in (
            "statusBox",
            "processingStatusBox",
            "metricsStatusBox",
            "powerStatusBox",
            "clipboardAutoToggleBtn",
            "crmProcessingRunBtn",
            "crmMassEmailerRunBtn",
        ):
            with self.subTest(element_id=element_id):
                self.assertEqual(len(re.findall(rf"\bid=['\"]{element_id}['\"]", self.html)), 1)


if __name__ == "__main__":
    unittest.main()
