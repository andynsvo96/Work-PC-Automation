"""Single-order Sleeve Prints reachout workflow.

The Chrome extension supplies the selected design tabs and a requested method for
each sleeve.  This worker applies the CRM changes once, collects the View Invoice
link without sending the CRM invoice, then sends the Salesforce Additional
Requests email.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
import re
import sys
import time

import config as config_module

WORKERS_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKERS_DIR not in sys.path:
    sys.path.insert(0, WORKERS_DIR)

import crm_copyright_cancel as shared
from crm_stock_issue_extension import _verify_final_recipients


AUTOMATION_KEY = "sleeve_prints"
AUTOMATION_NAME = "crm.sleeve_prints"
DISPLAY_NAME = "Sleeve Prints"
SALESFORCE_TEMPLATE = str(
    getattr(config_module, "SALESFORCE_SLEEVE_PRINTS_TEMPLATE", "[AUTO] Additional Requests")
    or "[AUTO] Additional Requests"
).strip()
ORDER_NUMBER_PLACEHOLDER = "[ORDER-NUMBER]"
REQUEST_PLACEHOLDER = "[REQUEST]"
COST_PLACEHOLDER = "[COST]"
INVOICE_LINK_PLACEHOLDER = "[INVOICE_LINK]"
INK = "ink"
EMBROIDERY = "embroidery"
SLEEVE_SIDES = ("left", "right")
MONEY_QUANTUM = Decimal("0.01")
MAX_SLEEVE_PRICE = Decimal("1000.00")


class SleevePrintsError(RuntimeError):
    """Raised when the Sleeve Prints workflow cannot safely continue."""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = dict(result or {})


SLEEVE_PRINTS_PROCESS = shared.CancelProcess(
    key=AUTOMATION_KEY,
    issue_type=DISPLAY_NAME,
    salesforce_template=SALESFORCE_TEMPLATE,
    template_search=SALESFORCE_TEMPLATE,
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=(),
    body_markers=("[request]", "[cost]", "[invoice_link]"),
    display_name=DISPLAY_NAME,
    requires_reason=False,
    cancel_and_refund=False,
)


def _whole_number(value, label, maximum=1_000_000):
    if isinstance(value, bool):
        raise SleevePrintsError(f"{label} must be a positive whole number.")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SleevePrintsError(f"{label} must be a positive whole number.") from None
    if number <= 0 or number > maximum:
        raise SleevePrintsError(f"{label} must be between 1 and {maximum:,}.")
    return number


def _money(value, label, *, required=False):
    if value in (None, ""):
        if required:
            raise SleevePrintsError(f"{label} is required.")
        return None
    try:
        amount = Decimal(str(value).strip()).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise SleevePrintsError(f"{label} must be a dollar amount with no more than two decimals.") from None
    if amount < 0 or amount > MAX_SLEEVE_PRICE:
        raise SleevePrintsError(f"{label} must be between $0.00 and ${MAX_SLEEVE_PRICE:.2f}.")
    return amount


def _money_text(value):
    return f"${Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP):.2f}"


def _ink_price_for_quantity(quantity):
    quantity = _whole_number(quantity, "Combined ink-print quantity")
    if quantity >= 100:
        return Decimal("5.00")
    if quantity >= 20:
        return Decimal("6.00")
    if quantity >= 10:
        return Decimal("7.00")
    return Decimal("8.00")


def normalize_request(sleeves, ink_price=None, embroidery_price=None):
    if not isinstance(sleeves, list) or not sleeves:
        raise SleevePrintsError("Select at least one design tab and sleeve request.")
    if len(sleeves) > 100:
        raise SleevePrintsError("Select no more than 100 design tabs.")
    normalized = []
    seen_tabs = set()
    any_ink = False
    any_embroidery = False
    for raw in sleeves:
        if not isinstance(raw, dict):
            raise SleevePrintsError("Each sleeve selection must be a valid design-tab record.")
        tab_number = _whole_number(raw.get("tab_number"), "Design tab number", maximum=1000)
        if tab_number in seen_tabs:
            raise SleevePrintsError(f"Design tab {tab_number} was selected more than once.")
        seen_tabs.add(tab_number)
        quantity = _whole_number(raw.get("quantity"), f"Design tab {tab_number} quantity")
        selection = {"tab_number": tab_number, "quantity": quantity}
        for side in SLEEVE_SIDES:
            method = str(raw.get(side) or "").strip().lower()
            if method not in {"", INK, EMBROIDERY}:
                raise SleevePrintsError(f"Design tab {tab_number} {side} sleeve has an unsupported method.")
            selection[side] = method
            any_ink = any_ink or method == INK
            any_embroidery = any_embroidery or method == EMBROIDERY
        if not selection["left"] and not selection["right"]:
            raise SleevePrintsError(f"Choose an ink-print or embroidery request for design tab {tab_number}.")
        normalized.append(selection)
    custom_ink = _money(ink_price, "Custom ink-print price")
    custom_embroidery = _money(embroidery_price, "Custom embroidery price")
    if custom_ink is not None and not any_ink:
        raise SleevePrintsError("A custom ink-print price was supplied without an ink-print sleeve.")
    if custom_embroidery is not None and not any_embroidery:
        raise SleevePrintsError("A custom embroidery price was supplied without an embroidery sleeve.")
    return {
        "sleeves": normalized,
        "ink_price": None if custom_ink is None else f"{custom_ink:.2f}",
        "embroidery_price": None if custom_embroidery is None else f"{custom_embroidery:.2f}",
    }


def _request_flags(sleeves):
    sleeves = sleeves or []
    return {
        "ink": any(selection.get(side) == INK for selection in sleeves for side in SLEEVE_SIDES),
        "embroidery": any(selection.get(side) == EMBROIDERY for selection in sleeves for side in SLEEVE_SIDES),
    }


def _format_request_text(sleeves):
    flags = _request_flags(sleeves)
    if flags["ink"] and flags["embroidery"]:
        return "sleeve print and embroidery"
    if flags["ink"]:
        return "sleeve prints"
    return "sleeve embroidery"


def _format_cost_text(sleeves, ink_price, embroidery_price):
    flags = _request_flags(sleeves)
    if flags["ink"] and flags["embroidery"]:
        return f"{_money_text(ink_price)} for sleeve prints and {_money_text(embroidery_price)} for embroidery"
    return _money_text(ink_price if flags["ink"] else embroidery_price)


def format_sales_note(sleeves, ink_price, embroidery_price):
    flags = _request_flags(sleeves)
    if flags["ink"] and flags["embroidery"]:
        return (
            "Sleeve prints and embroidery\n"
            f"Priced at {_money_text(ink_price)} for ink prints and {_money_text(embroidery_price)} for embroidery per sleeve\n"
            "Emailed Txted"
        )
    if flags["ink"]:
        return f"Sleeve prints\nPriced at {_money_text(ink_price)} per sleeve\nEmailed Txted"
    return f"Sleeve embroidery\nPriced at {_money_text(embroidery_price)} per sleeve\nEmailed Txted"


def _read_crm_sleeve_state(driver):
    return shared._order_scope(
        driver,
        r"""
        function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
        function ownOrScopeFunction(name) {
          for (let scope = s, depth = 0; scope && depth < 12; scope = scope.$parent, depth += 1) {
            if (typeof scope[name] === 'function') return scope[name].bind(scope);
          }
          return null;
        }
        const setAreaDescription = ownOrScopeFunction('setPrintAreaTemplateDescription');
        const setMethodDescription = ownOrScopeFunction('setPrintMethodDescription');
        function areaDescription(area) {
          const values = [
            area && area.description, area && area.printAreaDescription,
            area && area.printArea && area.printArea.description,
            area && area.printAreaTemplate && area.printAreaTemplate.description
          ];
          if (!values.some((value) => clean(value)) && setAreaDescription) {
            try { values.push(setAreaDescription(area)); } catch (error) {}
          }
          return clean(values.find((value) => clean(value)));
        }
        function methodDescription(area) {
          const values = [
            area && area.printMethodDescription, area && area.methodDescription,
            area && area.printMethod && area.printMethod.description,
            area && area.printMethodTemplate && area.printMethodTemplate.description
          ];
          if (!values.some((value) => clean(value)) && setMethodDescription) {
            try { values.push(setMethodDescription(area)); } catch (error) {}
          }
          return clean(values.find((value) => clean(value)));
        }
        function designQuantity(design) {
          const direct = Number(design && (design.quantity ?? design.totalQuantity ?? design.qty));
          if (Number.isFinite(direct) && direct > 0) return direct;
          let total = 0;
          for (const item of (design && design.designItems) || []) {
            for (const size of item.sizes || []) total += Math.max(0, Number(size.quantity) || 0);
          }
          return total;
        }
        return {
          sales_notes: String(r.salesNotes || r.addSalesNotes || ''),
          designs: (r.designs || []).map((design, index) => ({
            tab_number: index + 1,
            quantity: designQuantity(design),
            print_areas: (design.printAreas || [])
              .filter((area) => area && area.crudAction !== 'd')
              .map((area) => ({ description: areaDescription(area), method: methodDescription(area) }))
          }))
        };
        """,
    ) or {}


def _build_live_plan(request, state):
    designs_by_tab = {
        int(design.get("tab_number") or 0): design
        for design in (state.get("designs") or [])
        if int(design.get("tab_number") or 0) > 0
    }
    selections = []
    warnings = []
    for selection in request["sleeves"]:
        tab_number = int(selection["tab_number"])
        design = designs_by_tab.get(tab_number)
        if not design:
            raise SleevePrintsError(f"Selected design tab {tab_number} is no longer on the CRM order.")
        quantity = _whole_number(design.get("quantity"), f"Live quantity for design tab {tab_number}")
        methods = {str(area.get("method") or "").strip().lower() for area in (design.get("print_areas") or [])}
        existing_ink = {
            method for method in methods if method in {"hd digital", "screen printing"}
        }
        if len(existing_ink) > 1:
            ink_method = "HD Digital"
            warnings.append(
                f"Tab {tab_number} has both HD Digital and Screen Printing areas; Sleeve Prints used HD Digital."
            )
        elif "screen printing" in existing_ink:
            ink_method = "Screen Printing"
        else:
            ink_method = "HD Digital"
        selections.append({
            **selection,
            "quantity": quantity,
            "ink_method": ink_method,
            "existing_areas": list(design.get("print_areas") or []),
        })
    flags = _request_flags(selections)
    ink_quantity = sum(
        int(selection["quantity"])
        for selection in selections
        if selection.get("left") == INK or selection.get("right") == INK
    )
    ink_price = _money(request.get("ink_price"), "Custom ink-print price") if flags["ink"] else None
    if ink_price is None and flags["ink"]:
        ink_price = _ink_price_for_quantity(ink_quantity)
    embroidery_price = _money(request.get("embroidery_price"), "Custom embroidery price") if flags["embroidery"] else None
    if embroidery_price is None and flags["embroidery"]:
        embroidery_price = Decimal("15.00")
    for selection in selections:
        surcharge = Decimal("0.00")
        for side in SLEEVE_SIDES:
            if selection.get(side) == INK:
                surcharge += ink_price
            elif selection.get(side) == EMBROIDERY:
                surcharge += embroidery_price
        selection["surcharge"] = surcharge.quantize(MONEY_QUANTUM)
    return {
        "selections": selections,
        "ink_quantity": ink_quantity,
        "ink_price": ink_price,
        "embroidery_price": embroidery_price,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _crm_note_exists(state, note):
    return str(note or "").casefold() in str((state or {}).get("sales_notes") or "").casefold()


def _apply_crm_sleeve_changes(driver, plan, sales_note):
    payload = {
        "selections": [
            {
                "tab_number": selection["tab_number"],
                "left": selection["left"],
                "right": selection["right"],
                "ink_method": selection["ink_method"],
                "surcharge": f"{selection['surcharge']:.2f}",
            }
            for selection in plan["selections"]
        ],
        "sales_note": sales_note,
    }
    result = shared._order_scope(
        driver,
        r"""
        const request = arguments[0];
        function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
        function lower(value) { return clean(value).toLowerCase(); }
        function findScopeFunction(name) {
          for (let scope = s, depth = 0; scope && depth < 12; scope = scope.$parent, depth += 1) {
            if (typeof scope[name] === 'function') return scope[name].bind(scope);
          }
          return null;
        }
        function findStatics() {
          for (let scope = s, depth = 0; scope && depth < 12; scope = scope.$parent, depth += 1) {
            if (scope.STATICS) return scope.STATICS;
          }
          return window.STATICS || {};
        }
        function areaDescription(area) {
          const values = [
            area && area.description, area && area.printAreaDescription,
            area && area.printArea && area.printArea.description,
            area && area.printAreaTemplate && area.printAreaTemplate.description
          ];
          const setter = findScopeFunction('setPrintAreaTemplateDescription');
          if (!values.some((value) => clean(value)) && setter) {
            try { values.push(setter(area)); } catch (error) {}
          }
          return clean(values.find((value) => clean(value)));
        }
        function methodDescription(area) {
          const values = [
            area && area.printMethodDescription, area && area.methodDescription,
            area && area.printMethod && area.printMethod.description,
            area && area.printMethodTemplate && area.printMethodTemplate.description
          ];
          const setter = findScopeFunction('setPrintMethodDescription');
          if (!values.some((value) => clean(value)) && setter) {
            try { values.push(setter(area)); } catch (error) {}
          }
          return clean(values.find((value) => clean(value)));
        }
        function findByDescription(values, description) {
          const wanted = lower(description);
          return (values || []).find((value) => lower(value && value.description) === wanted) || null;
        }
        function activeAreas(design) {
          return (design.printAreas || []).filter((area) => area && area.crudAction !== 'd');
        }
        function existingArea(design, description) {
          return activeAreas(design).find((area) => lower(areaDescription(area)) === lower(description)) || null;
        }
        function price(value) {
          const number = Number(value);
          if (!Number.isFinite(number) || number < 0) throw new Error(`CRM price is invalid: ${value}`);
          return number;
        }
        const addPrintArea = findScopeFunction('addPrintArea');
        const updateAreaTemplate = findScopeFunction('updateAreaTemplate');
        const updateAreaMethod = findScopeFunction('updateAreaMethod');
        const watchItemChanges = findScopeFunction('watchItemChanges');
        const watchSizeChanges = findScopeFunction('watchSizeChanges');
        const statics = findStatics();
        const templates = statics.printAreaTemplates || [];
        const methods = statics.printMethods || [];
        if (!addPrintArea || !updateAreaTemplate || !updateAreaMethod) throw new Error('CRM print-area controls are unavailable.');
        const addedAreas = [];
        const existingAreas = [];
        const priceUpdates = [];
        runInAngular(s, () => {
          if (!s.editMode && typeof s.editModeOn === 'function') s.editModeOn();
          for (const selection of request.selections) {
            const designIndex = Number(selection.tab_number) - 1;
            const design = (r.designs || [])[designIndex];
            if (!design) throw new Error(`Design tab ${selection.tab_number} was not found while applying Sleeve Prints.`);
            for (const side of ['left', 'right']) {
              const requested = selection[side];
              if (!requested) continue;
              const description = side === 'left' ? 'Sleeve Left' : 'Sleeve Right';
              const expectedMethod = requested === 'embroidery' ? 'Embroidery' : selection.ink_method;
              const existing = existingArea(design, description);
              if (existing) {
                const actualMethod = methodDescription(existing);
                const compatible = requested === 'embroidery'
                  ? lower(actualMethod) === 'embroidery'
                  : ['hd digital', 'screen printing'].includes(lower(actualMethod));
                if (!compatible) {
                  throw new Error(`Tab ${selection.tab_number} already has ${description} using ${actualMethod || 'an unknown method'}, which conflicts with the requested ${expectedMethod}.`);
                }
                existingAreas.push({tab_number: selection.tab_number, description, method: actualMethod});
                continue;
              }
              const template = findByDescription(templates, description);
              const method = findByDescription(methods, expectedMethod);
              if (!template) throw new Error(`CRM does not expose the ${description} print-area template.`);
              if (!method) throw new Error(`CRM does not expose the ${expectedMethod} print method.`);
              const beforeCount = activeAreas(design).length;
              addPrintArea(design);
              const newArea = activeAreas(design)[activeAreas(design).length - 1];
              if (!newArea || activeAreas(design).length <= beforeCount) throw new Error(`CRM did not create ${description} on tab ${selection.tab_number}.`);
              updateAreaTemplate(newArea, template);
              updateAreaMethod(newArea, method);
              addedAreas.push({tab_number: selection.tab_number, description, method: expectedMethod});
            }
            const surcharge = price(selection.surcharge);
            let adjusted = 0;
            for (let itemIndex = 0; itemIndex < (design.designItems || []).length; itemIndex += 1) {
              const item = design.designItems[itemIndex];
              if (!item || item.crudAction === 'd') continue;
              if (Number(item.splitIntoSizes) === 0) {
                const next = Math.round((price(item.pricePerPiece) + surcharge) * 100) / 100;
                item.pricePerPiece = next.toFixed(2);
                if (watchItemChanges) watchItemChanges(item);
                priceUpdates.push({tab_number: selection.tab_number, item_index: itemIndex, size_index: null, price: next.toFixed(2)});
                adjusted += 1;
                continue;
              }
              for (let sizeIndex = 0; sizeIndex < (item.sizes || []).length; sizeIndex += 1) {
                const size = item.sizes[sizeIndex];
                if (!size || size.crudAction === 'd' || Number(size.quantity) <= 0) continue;
                const next = Math.round((price(size.pricePerPiece) + surcharge) * 100) / 100;
                size.pricePerPiece = next.toFixed(2);
                if (watchSizeChanges) watchSizeChanges(item, size);
                priceUpdates.push({tab_number: selection.tab_number, item_index: itemIndex, size_index: sizeIndex, price: next.toFixed(2)});
                adjusted += 1;
              }
            }
            if (!adjusted) throw new Error(`Tab ${selection.tab_number} has no active product size price to update.`);
          }
          const existingDraft = String(r.addSalesNotes || '').trim();
          if (!existingDraft.toLowerCase().includes(request.sales_note.toLowerCase())) {
            r.addSalesNotes = [existingDraft, request.sales_note].filter(Boolean).join('\n');
            if (s.order && typeof s.order.setAddSalesNotes === 'function') s.order.setAddSalesNotes(r.addSalesNotes);
          }
        });
        return {added_areas: addedAreas, existing_areas: existingAreas, price_updates: priceUpdates};
        """,
        payload,
    ) or {}
    return result


def _verify_crm_sleeve_changes(driver, sales_note, mutation):
    expected_areas = list((mutation or {}).get("added_areas") or []) + list((mutation or {}).get("existing_areas") or [])
    expected_prices = list((mutation or {}).get("price_updates") or [])
    verification = shared._order_scope(
        driver,
        r"""
        const note = arguments[0];
        const expectedAreas = arguments[1];
        const expectedPrices = arguments[2];
        function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
        function lower(value) { return clean(value).toLowerCase(); }
        function areaDescription(area) {
          return clean(area && (area.description || area.printAreaDescription || (area.printArea || {}).description || (area.printAreaTemplate || {}).description));
        }
        function methodDescription(area) {
          return clean(area && (area.printMethodDescription || area.methodDescription || (area.printMethod || {}).description || (area.printMethodTemplate || {}).description));
        }
        const missingAreas = [];
        for (const expected of expectedAreas) {
          const design = (r.designs || [])[Number(expected.tab_number) - 1];
          const matched = design && (design.printAreas || []).some((area) => area && area.crudAction !== 'd'
            && lower(areaDescription(area)) === lower(expected.description)
            && lower(methodDescription(area)) === lower(expected.method));
          if (!matched) missingAreas.push(expected);
        }
        const incorrectPrices = [];
        for (const expected of expectedPrices) {
          const design = (r.designs || [])[Number(expected.tab_number) - 1];
          const item = design && (design.designItems || [])[Number(expected.item_index)];
          const actual = expected.size_index === null
            ? item && item.pricePerPiece
            : item && (item.sizes || [])[Number(expected.size_index)] && (item.sizes || [])[Number(expected.size_index)].pricePerPiece;
          if (Number(actual) !== Number(expected.price)) incorrectPrices.push({...expected, actual});
        }
        const salesNotes = String(r.salesNotes || r.addSalesNotes || '');
        return {
          note_saved: salesNotes.toLowerCase().includes(String(note || '').toLowerCase()),
          missing_areas: missingAreas,
          incorrect_prices: incorrectPrices
        };
        """,
        sales_note,
        expected_areas,
        expected_prices,
    ) or {}
    if not verification.get("note_saved"):
        raise SleevePrintsError("CRM saved the order, but the Sleeve Prints Sales Note was not confirmed afterward.")
    if verification.get("missing_areas"):
        raise SleevePrintsError(f"CRM saved the order, but these Sleeve Print Areas were not confirmed: {verification['missing_areas']}")
    if verification.get("incorrect_prices"):
        raise SleevePrintsError(f"CRM saved the order, but these Sleeve prices were not confirmed: {verification['incorrect_prices']}")
    return verification


def _capture_view_invoice_link(driver):
    shared._activate_crm_context(driver)
    opened = driver.execute_script(
        r"""
        function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        const controls = Array.from(document.querySelectorAll('button,input[type=button],input[type=submit],a,[role=button]'))
          .filter((el) => visible(el) && clean(el.value || el.innerText || el.textContent || el.getAttribute('aria-label')) === 'send invoice');
        if (!controls.length) return false;
        controls.sort((a, b) => {
          const ar = a.getBoundingClientRect(); const br = b.getBoundingClientRect();
          return (br.width * br.height) - (ar.width * ar.height);
        });
        const control = controls[0];
        // automation_runtime intentionally blocks CRM invoice sends.  Mark
        // this one click so the dialog can be opened and safely cancelled.
        control.dataset.automationAllowClick = 'true';
        try {
          control.scrollIntoView({block: 'center', inline: 'center'});
          control.click();
          return true;
        } finally {
          delete control.dataset.automationAllowClick;
        }
        """
    )
    if not opened:
        raise SleevePrintsError("CRM Send Invoice button was not found.")
    # The legacy CRM currently renders this modal inside its app iframe.  Some
    # deployments place it in the parent document, so check the active CRM
    # context first and then fall back to the parent without clicking Send a
    # second time.
    deadline = time.monotonic() + 20
    href = ""
    link_context = ""
    while time.monotonic() < deadline:
        shared._activate_crm_context(driver)
        href = str(driver.execute_script(
            r"""
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            }
            const links = Array.from(document.querySelectorAll('a[href]')).filter((link) => visible(link));
            const view = links.find((link) => {
              const href = String(link.href || '');
              return /order-invoice/i.test(href) && !/\.pdf(?:$|[?#])/i.test(href);
            });
            return view ? String(view.href || '') : '';
            """
        ) or "").strip()
        if href:
            link_context = "crm"
            break
        driver.switch_to.default_content()
        href = str(driver.execute_script(
            r"""
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            }
            const links = Array.from(document.querySelectorAll('a[href]')).filter((link) => visible(link));
            const view = links.find((link) => {
              const href = String(link.href || '');
              return /order-invoice/i.test(href) && !/\.pdf(?:$|[?#])/i.test(href);
            });
            return view ? String(view.href || '') : '';
            """
        ) or "").strip()
        if href:
            link_context = "parent"
            break
        time.sleep(0.4)
    if not href or ".pdf" in href.lower():
        raise SleevePrintsError("CRM invoice popup did not expose a non-PDF View Invoice link.")
    if link_context == "crm":
        shared._activate_crm_context(driver)
    else:
        driver.switch_to.default_content()
    cancelled = driver.execute_script(
        r"""
        function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
        function visible(el) {
          const rect = el.getBoundingClientRect(); const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        const invoiceHref = arguments[0];
        const dialogs = Array.from(document.querySelectorAll('[role=dialog], .modal, .modal-dialog, .modal-content, .uiModal, div'))
          .filter((el) => visible(el) && Array.from(el.querySelectorAll('a[href]')).some((link) => String(link.href || '') === invoiceHref));
        for (const dialog of dialogs.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)) {
          const button = Array.from(dialog.querySelectorAll('button,a,input,[role=button]'))
            .find((el) => visible(el) && clean(el.value || el.innerText || el.textContent || el.getAttribute('aria-label')) === 'cancel');
          if (button) { button.click(); return true; }
        }
        return false;
        """,
        href,
    )
    if not cancelled:
        raise SleevePrintsError("CRM View Invoice link was found, but the invoice popup Cancel button was not found.")
    shared._activate_crm_context(driver)
    return href


def _replace_additional_request_placeholders(driver, request_text, cost_text, invoice_link):
    replacements = {
        REQUEST_PLACEHOLDER: request_text,
        COST_PLACEHOLDER: cost_text,
        INVOICE_LINK_PLACEHOLDER: invoice_link,
    }
    result = driver.execute_script(
        r"""
        const replacements = arguments[0];
        function visible(el) {
          if (!el || !el.getBoundingClientRect) return false;
          const rect = el.getBoundingClientRect();
          const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
          const style = view.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        function escapeRegExp(value) { return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
        const counts = Object.fromEntries(Object.keys(replacements).map((key) => [key, 0]));
        const seenDocuments = new Set();
        function replaceRoot(root) {
          if (!root) return;
          const doc = root.ownerDocument || document;
          const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
          const nodes = [];
          while (walker.nextNode()) nodes.push(walker.currentNode);
          for (const node of nodes) {
            let value = String(node.nodeValue || '');
            let changed = false;
            for (const [placeholder, replacement] of Object.entries(replacements)) {
              const pattern = new RegExp(escapeRegExp(placeholder), 'gi');
              const matches = value.match(pattern) || [];
              if (matches.length) {
                counts[placeholder] += matches.length;
                value = value.replace(pattern, String(replacement));
                changed = true;
              }
            }
            if (changed) node.nodeValue = value;
          }
          try { root.dispatchEvent(new Event('input', {bubbles: true})); root.dispatchEvent(new Event('change', {bubbles: true})); } catch (error) {}
        }
        function inspectDocument(doc) {
          if (!doc || seenDocuments.has(doc)) return;
          seenDocuments.add(doc);
          try {
            const editors = Object.values((doc.defaultView.CKEDITOR && doc.defaultView.CKEDITOR.instances) || {});
            for (const editor of editors) {
              const editable = editor.editable && editor.editable();
              if (editable && editable.$) {
                replaceRoot(editable.$);
                if (editor.updateElement) editor.updateElement();
                if (editor.fire) editor.fire('change');
              }
            }
          } catch (error) {}
          if (doc.body) replaceRoot(doc.body);
          for (const frame of Array.from(doc.querySelectorAll('iframe')).filter(visible)) {
            try { inspectDocument(frame.contentDocument || (frame.contentWindow && frame.contentWindow.document)); } catch (error) {}
          }
        }
        inspectDocument(document);
        return counts;
        """,
        replacements,
    ) or {}
    if int(result.get(REQUEST_PLACEHOLDER) or 0) != 2:
        raise SleevePrintsError(f"Salesforce template must contain exactly two {REQUEST_PLACEHOLDER} placeholders.")
    for placeholder in (COST_PLACEHOLDER, INVOICE_LINK_PLACEHOLDER):
        if int(result.get(placeholder) or 0) < 1:
            raise SleevePrintsError(f"Salesforce template body does not contain {placeholder}.")
    return result


def _prepare_and_send_salesforce_email(driver, crm_handle, order_id, customer_email, request_text, cost_text, invoice_link, *, dry_run=False, login_wait_seconds=0):
    sf_handle = shared._open_salesforce_account(
        driver, crm_handle, customer_email, login_wait_seconds=login_wait_seconds, order_id=order_id
    )
    shared._verify_salesforce_email(driver, customer_email)
    shared._click_salesforce_email(driver, customer_email)
    shared._wait_for_email_composer(driver)
    selected_from = shared._set_salesforce_from_orders(driver)
    shared._insert_cancel_template(driver, SLEEVE_PRINTS_PROCESS)
    deadline = time.monotonic() + 20
    state = {}
    while time.monotonic() < deadline:
        state = shared._read_salesforce_email_state(driver) or {}
        body = shared._clean_text(state.get("body"))
        subject = shared._clean_text(state.get("subject"))
        if subject and all(token.casefold() in body.casefold() for token in (REQUEST_PLACEHOLDER, COST_PLACEHOLDER, INVOICE_LINK_PLACEHOLDER)):
            break
        time.sleep(0.4)
    else:
        raise SleevePrintsError(
            f"Salesforce template {SALESFORCE_TEMPLATE} did not load with {REQUEST_PLACEHOLDER}, {COST_PLACEHOLDER}, and {INVOICE_LINK_PLACEHOLDER}."
        )
    if not shared._subject_has_order_placeholder(subject) and str(order_id) not in subject:
        raise SleevePrintsError(f"Salesforce template subject does not contain {ORDER_NUMBER_PLACEHOLDER}.")
    if str(order_id) not in subject:
        shared._replace_subject_order_number(driver, order_id)
    replacement = _replace_additional_request_placeholders(driver, request_text, cost_text, invoice_link)
    time.sleep(0.5)
    state = shared._read_salesforce_email_state(driver) or {}
    final_subject = shared._clean_text(state.get("subject"))
    final_body = shared._clean_text(state.get("body"))
    unresolved = [
        token for token in (REQUEST_PLACEHOLDER, COST_PLACEHOLDER, INVOICE_LINK_PLACEHOLDER)
        if token.casefold() in f"{final_subject}\n{final_body}".casefold()
    ]
    if unresolved:
        raise SleevePrintsError(f"Salesforce email still contains unresolved placeholders: {', '.join(unresolved)}.")
    if str(order_id) not in final_subject:
        raise SleevePrintsError("Salesforce email subject did not retain the CRM order number.")
    for expected in (request_text, cost_text, invoice_link):
        if expected.casefold() not in final_body.casefold():
            raise SleevePrintsError("Salesforce email body did not retain the Sleeve Prints request, cost, and invoice link.")
    recipients = _verify_final_recipients(driver, customer_email)
    if dry_run:
        return {
            "sent": False, "dry_run": True, "salesforce_handle": sf_handle, "from": selected_from,
            "recipients": recipients, "subject": final_subject, "body": final_body, "replacement": replacement,
        }
    if not shared._click_salesforce_send_button(driver):
        raise SleevePrintsError("Salesforce Send button was not found.")
    return {
        "sent": True, "dry_run": False, "salesforce_handle": sf_handle, "from": selected_from,
        "recipients": recipients, "subject": final_subject, "body": final_body, "replacement": replacement,
    }


def process_sleeve_prints_order(
    order_id,
    sleeves,
    ink_price=None,
    embroidery_price=None,
    *,
    dry_run=False,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
    progress_callback=None,
):
    order_id = shared._normalize_order_id(order_id)
    request = normalize_request(sleeves, ink_price, embroidery_price)
    result = {
        "success": False, "order_id": order_id, "automation": AUTOMATION_KEY, "request": request,
        "dry_run": bool(dry_run), "stages": [], "warnings": [],
    }
    driver = None
    stage = "browser_start"

    def begin(key, message):
        nonlocal stage
        stage = key
        if callable(progress_callback):
            progress_callback(key, message)

    def complete(key, details):
        result["stages"].append({"key": key, "success": True, "details": details})

    try:
        begin("browser_start", f"Opening CRM order {order_id} for Sleeve Prints.")
        driver = shared._open_driver(visible=visible, attach_browser=attach_browser, debugger_address=debugger_address)
        order_url = shared.PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)
        shared.safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
        shared._login_to_crm_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
        shared._switch_to_crm_app_frame(driver)
        shared._wait_for_order_scope(driver, order_id=order_id)
        crm_handle = driver.current_window_handle
        contact = shared._wait_for_crm_contact_info(driver, order_id=order_id)
        complete("crm_order_verification", {"customer_email": contact["email"]})

        begin("crm_order_update", "Adding Sleeve Print Areas, pricing, and Sales Notes.")
        before_state = _read_crm_sleeve_state(driver)
        plan = _build_live_plan(request, before_state)
        sales_note = format_sales_note(plan["selections"], plan["ink_price"], plan["embroidery_price"])
        request_text = _format_request_text(plan["selections"])
        cost_text = _format_cost_text(plan["selections"], plan["ink_price"], plan["embroidery_price"])
        result.update({
            "plan": {
                "ink_quantity": plan["ink_quantity"],
                "ink_price": None if plan["ink_price"] is None else f"{plan['ink_price']:.2f}",
                "embroidery_price": None if plan["embroidery_price"] is None else f"{plan['embroidery_price']:.2f}",
                "selections": plan["selections"],
            },
            "warnings": plan["warnings"],
            "sales_note_text": sales_note,
            "request_text": request_text,
            "cost_text": cost_text,
        })
        if _crm_note_exists(before_state, sales_note):
            mutation = {"skipped": True, "reason": "matching_sales_note_already_saved", "added_areas": [], "existing_areas": [], "price_updates": []}
            result["crm_order_update"] = mutation
            complete("crm_order_update", mutation)
        else:
            mutation = _apply_crm_sleeve_changes(driver, plan, sales_note)
            save = shared._save_order_and_wait(driver)
            verification = _verify_crm_sleeve_changes(driver, sales_note, mutation)
            result["crm_order_update"] = {"mutation": mutation, "save": save, "verification": verification}
            complete("crm_order_update", result["crm_order_update"])

        begin("invoice_link", "Copying the CRM View Invoice link without sending the invoice.")
        invoice_link = _capture_view_invoice_link(driver)
        result["invoice_link"] = invoice_link
        complete("invoice_link", {"captured": True})

        begin("salesforce_email", "Preparing and sending the Additional Requests email.")
        salesforce = _prepare_and_send_salesforce_email(
            driver, crm_handle, order_id, contact["email"], request_text, cost_text, invoice_link,
            dry_run=dry_run, login_wait_seconds=login_wait_seconds,
        )
        result["salesforce"] = salesforce
        complete("salesforce_email", salesforce)
        result["success"] = True
        result["failed_stage"] = None
        return result
    except Exception as exc:
        if driver is not None:
            shared.safe_take_screenshot(driver, f"sleeve_prints_{order_id}_{stage}_error")
        result["failed_stage"] = stage
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        result["stages"].append({"key": stage, "success": False, "message": str(exc)})
        raise SleevePrintsError(f"Sleeve Prints stopped at {stage}: {exc}", result=result) from exc
    finally:
        if driver is not None and not attach_browser:
            shared.safe_driver_quit(driver, profile_path=shared._profile_path())


def run_sleeve_prints_order(order_id, sleeves, ink_price=None, embroidery_price=None, **kwargs):
    """Queue-friendly Sleeve Prints result tuple."""
    try:
        result = process_sleeve_prints_order(order_id, sleeves, ink_price, embroidery_price, **kwargs)
        warning = ""
        if result.get("warnings"):
            warning = " Warning: " + " ".join(result["warnings"])
        return True, f"Sleeve Prints completed for order {result['order_id']}.{warning}", result
    except SleevePrintsError as exc:
        return False, str(exc), exc.result
