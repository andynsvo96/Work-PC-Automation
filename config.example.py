"""Example machine-local configuration.

Copy this file to ``config.py``. All supported settings are inherited from the
tracked ``config_defaults.py`` module, so application updates can introduce a
new safe default without requiring every machine's ignored config file to be
rewritten immediately.
"""

from config_defaults import *  # noqa: F403,F401


# Add machine-specific overrides below. Common examples:
# AUTOMATION_REMOTE_ACCESS_MODE = "tailscale"
# AUTOMATION_APP_PIN_REQUIRED = True
# AUTOMATION_USE_LEGACY_PROFILES = True
# PAYCOM_PROFILE_DIR = r"C:\Users\you\AppData\Local\Google\Chrome\User Data"
# PAYCOM_REQUIRE_TRUSTED_SESSION = True
# SLACK_CHANNEL_URL = "https://app.slack.com/client/<workspace>/<channel>"
# CRM_REPORT_URL = "https://crm.example/report/..."
# CRM_PROFILE_DIR = "chrome_profile_crm"

# Secrets do not belong in this file. Store them with manage_credentials.py
# (or manage_windows_credentials.py on Windows).
