"""Validate Oversize EMB selections and keep print areas paired with designs."""
from pathlib import Path
import re

try:
    from .complicated_emb import normalize_designs as normalize_design_names, replace_email_body_pattern
except ImportError:
    from complicated_emb import normalize_designs as normalize_design_names, replace_email_body_pattern


_DESIGN_TOKEN = r"\[\s*DESIGN\s*\]"
# Keep the capture compatible with both Python re and JavaScript RegExp.
# Salesforce templates may wrap the Design Name in straight or smart quotes.
PRINT_DESIGN_PATTERN = (
    rf"\[\s*PRINT\s*\]\s+for\s+({_DESIGN_TOKEN}"
    rf'|"\s*{_DESIGN_TOKEN}\s*"|“\s*{_DESIGN_TOKEN}\s*”'
    rf"|'\s*{_DESIGN_TOKEN}\s*'|‘\s*{_DESIGN_TOKEN}\s*’)"
)
PLACEHOLDER_PATTERN = re.compile(r"\[\s*(?:PRINT|DESIGN|REASON|ORDER[\s_-]*NUMBER)\s*\]|XXXXXX", re.I)


def normalize_designs(designs):
    if not isinstance(designs, list) or not designs:
        raise ValueError("Select at least one design and print area for Oversize EMB.")
    names = normalize_design_names(designs)
    by_tab = {row["tab_number"]: row for row in designs}
    for row in names:
        areas = by_tab[row["tab_number"]].get("print_areas")
        if not isinstance(areas, list) or not areas or len(areas) > 100:
            raise ValueError("Select at least one print area for each Oversize EMB design.")
        cleaned = []
        for area in areas:
            if not isinstance(area, str):
                raise ValueError("Invalid Oversize EMB print area.")
            area = " ".join(area.split())
            if not area or len(area) > 500 or PLACEHOLDER_PATTERN.search(area):
                raise ValueError("Each print area needs a readable name without email placeholders.")
            if area.casefold() in {value.casefold() for value in cleaned}:
                raise ValueError("Select each print area only once per design.")
            cleaned.append(area)
        if PLACEHOLDER_PATTERN.search(row["design_name"]):
            raise ValueError("Design Name contains an unresolved email placeholder.")
        row["print_areas"] = cleaned
    return names


def _read_methods(driver):
    script = (Path(__file__).resolve().parents[1] / "crm-order-dark-mode-extension/emb-design-reader.js").read_text(encoding="utf-8")
    return driver.execute_script(script + "\nreturn readEmbDesignMethods();") or []


def resolve_designs(driver, selections=None):
    try:
        from .complicated_emb import resolve_designs as resolve_design_names
    except ImportError:
        from complicated_emb import resolve_designs as resolve_design_names

    if selections is None:
        eligible = [row for row in _read_methods(driver) if row.get("eligible")]
        if len(eligible) != 1 or len(eligible[0].get("print_areas", [])) != 1:
            raise ValueError("Choose the applicable designs and print areas in Reachout > Oversize EMB to HDD.")
        row = eligible[0]
        # Resolve the name through the existing stable, visible-tab scan.
        names = resolve_design_names(driver)
        if names[0]["tab_number"] != row["tab_number"]:
            raise ValueError("Available design tabs changed. Reopen Oversize EMB and select the areas again.")
        selections = [{**names[0], "print_areas": [row["print_areas"][0]["description"]]}]
    else:
        selections = normalize_designs(selections)
        resolve_design_names(driver, selections)
    methods = {row["tab_number"]: row for row in _read_methods(driver)}
    resolved = []
    for selected in normalize_designs(selections):
        number = selected["tab_number"]
        metadata = methods.get(number, {})
        areas = [area.get("description", "") for area in metadata.get("print_areas", [])]
        if (not metadata.get("eligible") or not areas or any(not area for area in areas)
                or len({area.casefold() for area in areas}) != len(areas)):
            raise ValueError(f"Could not read print areas and methods for design tab {number}.")
        for area in selected["print_areas"]:
            if areas.count(area) != 1:
                raise ValueError(f"Print area {area} changed or is ambiguous on tab {number}. Reopen Oversize EMB and select the areas again.")
        # Preserve CRM area order when the request's checkbox order differs.
        selected["print_areas"] = [area for area in areas if area in selected["print_areas"]]
        resolved.append(selected)
    return resolved


def _join(values):
    if len(values) < 3:
        return " and ".join(values)
    return ", ".join(values[:-1]) + ", and " + values[-1]


def _print_area_text(area):
    name = area.lower()
    return name if name.endswith(" print") else f"{name} print"


def print_design_text(designs, design_quotes=("", "")):
    opening, closing = design_quotes
    pairs = [f"{_join([_print_area_text(area) for area in row['print_areas']])} for {opening}{row['design_name']}{closing}"
             for row in normalize_designs(designs)]
    # The template already supplies "the" before [PRINT]. Repeat it for each
    # following design so different designs retain their own selected areas.
    return _join([pairs[0]] + [f"the {pair}" for pair in pairs[1:]])


def replace_print_design_placeholder(driver, replacement):
    return replace_email_body_pattern(driver, replacement, PRINT_DESIGN_PATTERN)
