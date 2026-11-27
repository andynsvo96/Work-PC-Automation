"""Report settings and target validation for CRM Processing."""

import re
from urllib.parse import urlsplit


CRM_RUSH_REPORT_KEYS = {
    "stock_unlocker": ("CRM_LOCKED_URL", "CRM_UNLOCKER_RUSH_CONTRACTED_URL"),
    "order_goods": ("CRM_ORDER_GOODS_RUSH_URL", "CRM_ORDER_GOODS_RUSH_CONTRACTED_URL"),
    "shipping_bypasser": ("CRM_SHIPPING_BYPASS_URL", "CRM_SHIPPING_BYPASS_RUSH_CONTRACTED_URL"),
    "push_back": ("CRM_PUSH_BACK_RUSH_URL", "CRM_PUSH_BACK_RUSH_CONTRACTED_URL"),
}

CRM_LINK_SETTING_KEYS = (
    "CRM_SHIPPING_FREE_URL", "PRODUCT_SEPARATOR_LIST_URL_FREE", "CRM_AUTO_SPLITTER_LIST_URL_FREE",
    "CRM_UNLOCKER_FREE_URL", "CRM_ORDER_GOODS_FREE_URL", "CRM_SHIPPING_BYPASS_FREE_URL", "CRM_PUSH_BACK_FREE_URL",
    "CRM_SHIPPING_RUSH_URL", "PRODUCT_SEPARATOR_LIST_URL_RUSH", "CRM_AUTO_SPLITTER_LIST_URL_RUSH",
    *(key for pair in CRM_RUSH_REPORT_KEYS.values() for key in pair),
    "CRM_SHIPPING_HIGH_VALUE_URL", "PRODUCT_SEPARATOR_LIST_URL_HIGH_VALUE", "CRM_AUTO_SPLITTER_LIST_URL_HIGH_VALUE",
    "CRM_UNLOCKER_HIGH_VALUE_URL", "CRM_ORDER_GOODS_HIGH_VALUE_URL", "CRM_SHIPPING_BYPASS_HIGH_VALUE_URL", "CRM_PUSH_BACK_HIGH_VALUE_URL",
    "CRM_SHIPPING_ALL_URL", "PRODUCT_SEPARATOR_LIST_URL_ALL", "CRM_AUTO_SPLITTER_LIST_URL_ALL",
    "CRM_UNLOCKER_ALL_URL", "CRM_ORDER_GOODS_ALL_URL", "CRM_SHIPPING_BYPASS_ALL_URL", "CRM_PUSH_BACK_ALL_URL",
    "CRM_813_VALIDATOR_URL", "CRM_813_ORDER_GOODS_URL", "CRM_813_BYPASS_URL", "CRM_PUSH_BACK_813_URL",
    "CRM_LOGIN_URL", "CRM_SHIPPING_URL", "PROCESSOR_LOGIN_URL", "PROCESSOR_LIST_URL",
    "PROCESSOR_ORDER_URL_TEMPLATE", "PRODUCT_SEPARATOR_LIST_URL",
)


def normalize_rush_contracted_steps(value, preferences):
    if not isinstance(value, list):
        return []
    return [
        step for step in CRM_RUSH_REPORT_KEYS
        if step in value and preferences.get(f"{step}_enabled")
    ]


def normalize_custom_crm_list_url(value):
    message = "Enter a valid CRM list link beginning with http:// or https://."
    if not isinstance(value, str):
        raise ValueError(message)
    url = value.strip()
    if not url or any(char.isspace() or ord(char) < 32 for char in url) or "\\" in url:
        raise ValueError(message)
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme.lower() in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
        )
        parts.port  # Reject malformed ports as well as malformed hosts.
    except ValueError:
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)
    return url


def normalize_custom_crm_order_ids(value):
    """Reject a malformed list in full, and preserve the first occurrence of each ID."""
    message = "Each entry must be a seven-digit order number or an individual CRM order link. Separate entries with commas, spaces, or new lines."
    if isinstance(value, str):
        items = [item for item in re.split(r"[\s,;]+", value.strip()) if item]
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError(message)
    if not items:
        raise ValueError("Enter at least one seven-digit order number or individual CRM order link.")
    order_ids = []
    for item in items:
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            raise ValueError(message)
        text = str(item).strip()
        match = re.fullmatch(r"#?([0-9]{7})", text)
        if match is None:
            try:
                parts = urlsplit(normalize_custom_crm_list_url(text))
            except ValueError:
                raise ValueError(message) from None
            route = parts.path
            if route.rstrip("/") == "/app":
                route = "/" + parts.fragment.split("?", 1)[0].lstrip("!/")
            elif parts.fragment.startswith(("/order/", "!/order/")):
                # Two order routes in one URL are ambiguous; do not pick one.
                raise ValueError(message)
            match = re.fullmatch(r"/order/([0-9]{7})/?", route)
            if match is None:
                raise ValueError(message)
        order_id = match.group(1)
        if order_id not in order_ids:
            order_ids.append(order_id)
    if len(order_ids) > 100:
        raise ValueError("Custom Processing accepts up to 100 unique order IDs per run.")
    return order_ids


def normalize_custom_crm_target(options=None, state=None):
    options = options or {}
    state = state or {}
    input_type = options.get("custom_input_type")
    if input_type is None:
        if options.get("custom_order_ids") is not None:
            input_type = "orders"
        elif options.get("custom_list_url") is not None:
            input_type = "link"
        else:
            input_type = state.get("custom_input_type") or "link"
    if not isinstance(input_type, str) or input_type not in {"link", "orders"}:
        raise ValueError("Choose a CRM link or an order ID list for Custom Processing.")
    field = "custom_order_ids" if input_type == "orders" else "custom_list_url"
    value = options.get(field) if options.get(field) is not None else state.get(field)
    normalize = normalize_custom_crm_order_ids if input_type == "orders" else normalize_custom_crm_list_url
    return {"custom_input_type": input_type, field: normalize(value)}
