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

    def test_each_connection_has_credential_setup_action(self):
        for service in ("paycom", "crm", "slack", "sanmar", "salesforce"):
            with self.subTest(service=service):
                self.assertIn(f"openCredentialSetup('{service}', this)", self.html)
        self.assertIn("function openCredentialSetup(", self.html)
        self.assertIn("/automation/credential-setup", self.html)

    def test_primary_navigation_is_consolidated(self):
        self.assertIn("id='tabBtnAutomation'", self.html)
        self.assertIn("id='tabBtnProcessing'", self.html)
        self.assertIn("id='tabBtnSystem'", self.html)
        self.assertNotIn("id='tabBtnMetrics'", self.html)
        self.assertNotIn("id='tabBtnPower'", self.html)
        self.assertIn("id='tabSystem'", self.html)

    def test_header_shows_a_live_build_indicator(self):
        self.assertEqual(len(re.findall(r"\bid=['\"]appVersionText['\"]", self.html)), 1)
        self.assertNotIn(".app-version{display:none}", self.html)
        self.assertIn(".app-version{display:inline-flex}", self.html)
        self.assertIn("Build: ${loadedCommit.slice(0, 7)} · Synced", self.html)
        self.assertIn("Build: ${loadedCommit.slice(0, 7)} → ${availableCommit.slice(0, 7)}", self.html)
        self.assertIn("Both computers should show this same build.", self.html)

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

    def test_processing_run_buttons_remain_queue_controls_while_a_run_is_active(self):
        self.assertIn("Add to Queue →", self.html)
        self.assertIn("Add Scanner to Queue", self.html)
        self.assertIn("const submitting = runBtn.dataset.busy === 'true';", self.html)
        self.assertIn(
            "setAutomationButtonRunning(runBtn, submitting || selectedSteps.length === 0);",
            self.html,
        )
        self.assertIn("setAutomationButtonRunning(runBtn, submitting);", self.html)
        self.assertNotIn("setAutomationButtonRunning(runBtn, running || selectedSteps.length === 0);", self.html)
        self.assertNotIn("? 'Running…'", self.html)

    def test_salesforce_worker_setup_is_wired(self):
        for element_id in ("salesforceWorkerSetupRows", "salesforceTestAllBtn", "salesforceSetupAllBtn"):
            self.assertEqual(len(re.findall(rf"\bid=['\"]{element_id}['\"]", self.html)), 1)
        for function_name in (
            "loadSalesforceWorkerSetupStatus",
            "setupSalesforceWorker",
            "testSalesforceWorker",
            "testAllSalesforceWorkers",
            "setupAllSalesforceWorkers",
        ):
            self.assertIn(f"function {function_name}(", self.html)
        self.assertIn("/api/salesforce-worker-setup", self.html)
        self.assertIn("/automation/salesforce-worker-setup", self.html)
        self.assertIn("/automation/salesforce-worker-test", self.html)

    def test_salesforce_verification_popup_is_wired(self):
        for element_id in (
            "salesforceVerificationDialog",
            "salesforceVerificationCodeInput",
            "salesforceVerificationSubmitBtn",
            "salesforceVerificationCancelBtn",
        ):
            self.assertEqual(len(re.findall(rf"\bid=['\"]{element_id}['\"]", self.html)), 1)
        for function_name in (
            "syncSalesforceVerificationPrompt",
            "pollSalesforceVerification",
            "submitSalesforceVerificationCode",
            "cancelSalesforceVerification",
        ):
            self.assertIn(f"function {function_name}(", self.html)
        self.assertIn("/api/salesforce-verification/submit", self.html)
        self.assertIn("Verify and resume", self.html)

    def test_system_sections_are_merged_and_wired(self):
        expected = {"overview", "hardware", "clipboard", "power"}
        sections = set(re.findall(r"data-system-section=['\"]([^'\"]+)", self.html))
        targets = set(re.findall(r"data-system-target=['\"]([^'\"]+)", self.html))
        self.assertEqual(sections, expected)
        self.assertEqual(targets, set())
        self.assertEqual(self.html.count("system-legacy-section"), 4)
        self.assertIn("data-windows-control", self.html)
        self.assertIn("function windowsControlsAvailable(", self.html)
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
            r"@media \(max-width:560px\)\{.*?\.tab-bar\{[^}]*grid-template-columns:repeat\(4,minmax\(0,1fr\)\)",
        )
        self.assertRegex(
            self.html,
            r"@media \(max-width:560px\)\{.*?\.rough-system-metrics\{grid-template-columns:1fr\}",
        )

    def test_communications_show_compact_week_calculator_and_today_slack_history(self):
        self.assertIn("Weekly schedule calculator", self.html)
        self.assertIn("id='workPredictionDays'", self.html)
        self.assertIn("id='communicationSlackHistory'", self.html)
        self.assertIn("function loadTodaySlackHistory(", self.html)

    def test_processing_reports_and_shared_schedule_controls_are_visible(self):
        self.assertNotRegex(self.html, r"<details[^>]*rough-advanced-details")
        self.assertIn("Optional controls apply to either Main automation or Sheet Scanner.", self.html)
        self.assertIn("id='crmProcessingLatestReportBtn'", self.html)
        self.assertIn("function openCrmProcessingRunReport(", self.html)
        self.assertIn(">View Report</button>", self.html)

    def test_main_run_history_is_paged_inside_history_section(self):
        reports_section = re.search(
            r"<section class='workspace-section' data-processing-section='reports'>(.*?)"
            r"<section class='workspace-section' data-processing-section='tools'>",
            self.html,
            re.S,
        )
        run_section = re.search(
            r"<section class='workspace-section active' data-processing-section='run'>(.*?)"
            r"<section class='workspace-section' data-processing-section='reports'>",
            self.html,
            re.S,
        )
        self.assertIsNotNone(reports_section)
        self.assertIsNotNone(run_section)
        self.assertIn("id='crmProcessingMainHistoryRows'", reports_section.group(1))
        self.assertNotIn("id='crmProcessingMainHistoryRows'", run_section.group(1))
        self.assertIn("const CRM_PROCESSING_HISTORY_PAGE_SIZE = 8;", self.html)
        self.assertIn("function changeCrmProcessingHistoryPage(", self.html)
        for element_id in (
            "crmProcessingMainHistoryPager",
            "crmProcessingMainHistoryPrevPageBtn",
            "crmProcessingMainHistoryNextPageBtn",
            "crmProcessingMainHistoryPageText",
            "crmProcessingMainHistoryMetaText",
        ):
            self.assertEqual(len(re.findall(rf"\bid=['\"]{element_id}['\"]", self.html)), 1)

    def test_repeat_queue_reuses_existing_main_and_scanner_run_reports(self):
        self.assertIn("function queuePersistedRepeatHistory(task)", self.html)
        self.assertIn("function queueRepeatHistoryRowsInWindow(task, history, historyIndexKey)", self.html)
        self.assertIn("const repeatCompletedGraceMs = Math.max(120000, durationMs + 60000)", self.html)
        self.assertIn("crmProcessingStatusPayload.state", self.html)
        self.assertIn("crmMassEmailerStatusPayload.state", self.html)
        self.assertIn("processing_history_index", self.html)
        self.assertIn("openCrmProcessingRunReport(${Number(row.processing_history_index)})", self.html)
        self.assertIn("scanner_history_index", self.html)
        self.assertIn("openCrmOrderIdsDialogFromButton(this)", self.html)
        self.assertIn("Orders ${scannerOrderCount} | Failed ${scannerFailures} | Skipped ${scannerSkipped}", self.html)

    def test_shipping_issue_history_messages_use_orange_attention_style(self):
        self.assertIn("--vscode-orange:#b45309", self.html)
        self.assertIn("--vscode-orange:#e5a54b", self.html)
        self.assertIn(".crm-address-shipping-issue-message{color:var(--vscode-orange)}", self.html)
        self.assertIn("function isCrmAddressShippingIssue(", self.html)
        self.assertIn("function buildCrmAddressShippingIssueMessagesHtml(", self.html)
        self.assertIn("if (outcome.startsWith('po_box_canada_shipping_issue_')) return 'Canada PO Box';", self.html)
        self.assertIn("const shippingIssueMessages = buildCrmAddressShippingIssueMessagesHtml(row, orderId);", self.html)

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

    def test_action_failures_remain_visible_after_dashboard_redesign(self):
        self.assertNotIn(".rough-status{display:none}", self.html)
        self.assertIn("id='actionFeedback'", self.html)
        self.assertIn("function showActionFeedback(", self.html)
        self.assertIn("showActionFeedback(msg);", self.html)

    def test_control_target_and_cloud_queue_controls_are_removed(self):
        self.assertNotIn("controlTargetSelect", self.html)
        self.assertNotIn("X-Automation-Target-Node", self.html)
        self.assertNotIn("queueResumeBtn", self.html)
        self.assertNotIn("reassignQueueTask", self.html)


if __name__ == "__main__":
    unittest.main()
