// The v1 bridge only verifies that the local Automation app is available. It
// does not send CRM data or store app PINs, browser cookies, or credentials.
// Privileged controls will require a separate, user-approved pairing flow.

export const LOCAL_AUTOMATION_ENDPOINT = "http://127.0.0.1:5123";
export const LOCAL_BRIDGE_STATUS_ENDPOINT = `${LOCAL_AUTOMATION_ENDPOINT}/api/extension/bridge/status`;
export const LOCAL_BRIDGE_PROTOCOL = "automation.chrome-extension.bridge/v1";
const REQUEST_TIMEOUT_MS = 2500;

export async function getLocalBridgeStatus() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(LOCAL_BRIDGE_STATUS_ENDPOINT, {
      method: "GET",
      cache: "no-store",
      credentials: "omit",
      signal: controller.signal
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload || payload.success !== true || payload.protocol !== LOCAL_BRIDGE_PROTOCOL) {
      throw new Error((payload && payload.message) || "Local app returned an unsupported bridge response.");
    }
    return {
      enabled: true,
      connected: true,
      endpoint: LOCAL_AUTOMATION_ENDPOINT,
      protocol: payload.protocol,
      message: payload.message
    };
  } catch (error) {
    const timedOut = error && error.name === "AbortError";
    return {
      enabled: true,
      connected: false,
      endpoint: LOCAL_AUTOMATION_ENDPOINT,
      message: timedOut
        ? "Could not reach the local Automation app in time."
        : "Local Automation app is not reachable. Start it, then try again."
    };
  } finally {
    clearTimeout(timeout);
  }
}
