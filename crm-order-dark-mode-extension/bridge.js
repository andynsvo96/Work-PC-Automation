// The v2 bridge keeps availability checks read-only and uses a separate,
// loopback-only pairing token for the explicit single-order control path.

export const LOCAL_AUTOMATION_ENDPOINT = "http://127.0.0.1:5123";
export const LOCAL_BRIDGE_STATUS_ENDPOINT = `${LOCAL_AUTOMATION_ENDPOINT}/api/extension/bridge/status`;
export const LOCAL_BRIDGE_PROTOCOL = "automation.chrome-extension.bridge/v2";
export const LOCAL_BRIDGE_PAIR_ENDPOINT = `${LOCAL_AUTOMATION_ENDPOINT}/api/extension/bridge/pair`;
export const LOCAL_ORDER_PROCESS_ENDPOINT = `${LOCAL_AUTOMATION_ENDPOINT}/api/extension/bridge/process-order`;
export const LOCAL_ORDER_PROCESS_STATUS_ENDPOINT = `${LOCAL_ORDER_PROCESS_ENDPOINT}/status`;
const REQUEST_TIMEOUT_MS = 2500;

async function bridgeFetch(endpoint, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(endpoint, {
      cache: "no-store",
      credentials: "omit",
      ...options,
      signal: controller.signal
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload) {
      throw new Error((payload && payload.message) || "Local Automation app rejected the request.");
    }
    return payload;
  } finally {
    clearTimeout(timeout);
  }
}

export async function getLocalBridgeStatus() {
  try {
    const payload = await bridgeFetch(LOCAL_BRIDGE_STATUS_ENDPOINT, { method: "GET" });
    if (payload.success !== true || payload.protocol !== LOCAL_BRIDGE_PROTOCOL) {
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
  }
}

export async function pairLocalBridge(pin) {
  return bridgeFetch(LOCAL_BRIDGE_PAIR_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pin: String(pin || "") })
  });
}

export async function startLocalOrderProcessing(token, orderId, shippingTooExpensive) {
  return bridgeFetch(LOCAL_ORDER_PROCESS_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${token}`
    },
    body: JSON.stringify({
      order_id: String(orderId || ""),
      shipping_too_expensive: shippingTooExpensive === true
    })
  });
}

export async function getLocalOrderProcessingStatus(token) {
  return bridgeFetch(LOCAL_ORDER_PROCESS_STATUS_ENDPOINT, {
    method: "GET",
    headers: { "Authorization": `Bearer ${token}` }
  });
}
