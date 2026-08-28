"""Single-order Stock Issue -> Suggest Different Size automation.

This reuses the verified stock-color workflow with a size-specific template and
placeholder.  The CRM run lock keeps the temporary workflow configuration
exclusive while an order is being processed.
"""

from __future__ import annotations

from contextlib import contextmanager
import threading

import config as config_module

from workers import crm_stock_issue_color as color


shared = color.shared
extension = color.extension
AUTOMATION_KEY = "stock_issue_size"
AUTOMATION_NAME = "crm.stock_issue_size"
DISPLAY_NAME = "Stock Issue - Suggest Different Size"
SALESFORCE_TEMPLATE = str(
    getattr(config_module, "SALESFORCE_STOCK_ISSUE_SIZE_TEMPLATE", "[AUTO] Stock - Size")
    or "[AUTO] Stock - Size"
).strip()
SIZE_PLACEHOLDER = "[SIZE]"
MAX_SIZES = 20


class StockIssueSizeError(color.StockIssueColorError):
    """Raised when Suggest Different Size must stop before its next stage."""


STOCK_SIZE_PROCESS = shared.CancelProcess(
    key=AUTOMATION_KEY,
    issue_type=DISPLAY_NAME,
    salesforce_template=SALESFORCE_TEMPLATE,
    template_search=SALESFORCE_TEMPLATE,
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=("urgent stock issue",),
    body_markers=(
        "[stock]",
        "currently out of stock",
        "available size",
        "such as [size]",
        "approve the size change",
        "rushordertees.com team",
    ),
    display_name=DISPLAY_NAME,
    requires_reason=False,
    cancel_and_refund=False,
)

_WORKFLOW_LOCK = threading.RLock()
_OVERRIDE_NAMES = (
    "AUTOMATION_KEY", "AUTOMATION_NAME", "DISPLAY_NAME", "SALESFORCE_TEMPLATE",
    "COLOR_PLACEHOLDER", "MAX_COLORS", "SUGGESTION_LABEL", "STOCK_COLOR_PROCESS",
    "StockIssueColorError",
)


@contextmanager
def _size_workflow():
    """Apply size settings to the shared color workflow for one call."""
    with _WORKFLOW_LOCK:
        original = {name: getattr(color, name) for name in _OVERRIDE_NAMES}
        try:
            color.AUTOMATION_KEY = AUTOMATION_KEY
            color.AUTOMATION_NAME = AUTOMATION_NAME
            color.DISPLAY_NAME = DISPLAY_NAME
            color.SALESFORCE_TEMPLATE = SALESFORCE_TEMPLATE
            color.COLOR_PLACEHOLDER = SIZE_PLACEHOLDER
            color.MAX_COLORS = MAX_SIZES
            color.SUGGESTION_LABEL = "size"
            color.STOCK_COLOR_PROCESS = STOCK_SIZE_PROCESS
            color.StockIssueColorError = StockIssueSizeError
            yield
        finally:
            for name, value in original.items():
                setattr(color, name, value)


def _size_message(exc):
    return str(exc).replace("Suggested colors", "Suggested sizes").replace(
        "suggested color", "suggested size"
    ).replace("colors", "sizes").replace("color", "size")


def normalize_suggested_sizes(sizes):
    with _size_workflow():
        try:
            return color.normalize_suggested_colors(sizes)
        except StockIssueSizeError as exc:
            raise StockIssueSizeError(_size_message(exc)) from exc


def normalize_request(sizes, products):
    with _size_workflow():
        try:
            payload = color.normalize_request(sizes, products)
        except StockIssueSizeError as exc:
            raise StockIssueSizeError(_size_message(exc)) from exc
    return {"sizes": payload["colors"], "products": payload["products"]}


def format_suggested_sizes(sizes):
    with _size_workflow():
        return color.format_suggested_colors(sizes)


def format_email_stock_text(products):
    return color.format_email_stock_text(products)


def format_sales_note(sizes, products):
    with _size_workflow():
        return color.format_sales_note(sizes, products)


def format_slack_message(order_id):
    with _size_workflow():
        return color.format_slack_message(order_id)


def _stock_size_template_signature_error(state):
    with _size_workflow():
        return color._stock_color_template_signature_error(state)


def _replace_stock_size_placeholders(driver, stock_text, size_text):
    with _size_workflow():
        return color._replace_stock_color_placeholders(driver, stock_text, size_text)


def process_stock_issue_size_order(order_id, sizes, products, **kwargs):
    request = normalize_request(sizes, products)
    with _size_workflow():
        try:
            result = color.process_stock_issue_color_order(
                order_id, request["sizes"], request["products"], **kwargs
            )
        except StockIssueSizeError as exc:
            result = dict(exc.result or {})
            if result:
                result["sizes"] = result.pop("colors", request["sizes"])
                result["size_text"] = result.pop("color_text", format_suggested_sizes(request["sizes"]))
                result["automation"] = AUTOMATION_KEY
            raise StockIssueSizeError(_size_message(exc), result=result) from exc
    result["sizes"] = result.pop("colors", request["sizes"])
    result["size_text"] = result.pop("color_text", format_suggested_sizes(request["sizes"]))
    result["automation"] = AUTOMATION_KEY
    return result


def run_stock_issue_size_order(order_id, sizes, products, **kwargs):
    try:
        result = process_stock_issue_size_order(order_id, sizes, products, **kwargs)
        return True, f"Suggest Different Size completed for order {result['order_id']}.", result
    except StockIssueSizeError as exc:
        return False, str(exc), exc.result
