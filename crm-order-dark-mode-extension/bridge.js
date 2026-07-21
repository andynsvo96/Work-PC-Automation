// This module deliberately performs no network activity in v1.  It defines the
// boundary that future CRM controls will use after the Automation app adds a
// dedicated, paired local API.  Do not put the app PIN, CRM credentials, or a
// browser session cookie in this extension.

export const LOCAL_AUTOMATION_ENDPOINT = "http://127.0.0.1:5123";

export function getLocalBridgeStatus() {
  return {
    enabled: false,
    endpoint: LOCAL_AUTOMATION_ENDPOINT,
    message: "The local Automation app bridge is reserved for a future paired release."
  };
}
