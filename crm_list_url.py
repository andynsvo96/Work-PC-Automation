"""Validation for Custom Processing links and explicit order lists."""

import re
from urllib.parse import urlsplit


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
    if isinstance(value, str):
        items = [item for item in re.split(r"[\s,;]+", value.strip()) if item]
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError("Enter a list of seven-digit CRM order IDs.")
    if not items:
        raise ValueError("Enter at least one seven-digit CRM order ID.")
    order_ids = []
    for item in items:
        if not isinstance(item, (str, int)) or isinstance(item, bool) or not re.fullmatch(r"[0-9]{7}", str(item).strip()):
            raise ValueError("Every CRM order ID must contain exactly seven digits. Separate IDs with commas, spaces, or new lines.")
        order_id = str(item).strip()
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
