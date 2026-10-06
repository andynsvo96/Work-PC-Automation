"""Validation for user-supplied CRM list links without altering query filters."""

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
