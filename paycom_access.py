"""Recognize Paycom's company IP allowlist restriction."""

import re


IP_BLOCK_CODE = "PAYCOM_IP_NOT_AUTHORIZED"


def is_paycom_ip_block(message):
    text = " ".join(str(message or "").lower().split())
    return IP_BLOCK_CODE.lower() in text or (
        "has not authorized your ip address" in text
        and "web time clock" in text
    )


def paycom_ip_block_message(page_text):
    if not is_paycom_ip_block(page_text):
        return None
    match = re.search(r"ip address\s*\(([^)]+)\)", page_text, re.IGNORECASE)
    address = f" ({match.group(1)})" if match else ""
    return (
        f"{IP_BLOCK_CODE}: Paycom has blocked your current/new IP address{address}: "
        "your company has not authorized it for Web Time Clock. "
        "No punch was recorded. Contact HR to authorize your IP address."
    )
