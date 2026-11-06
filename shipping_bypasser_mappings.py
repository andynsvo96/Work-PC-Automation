"""Validated storage for Shipping Bypasser CRM-to-SanMar mappings."""

from __future__ import annotations

import json
import os
import re
import threading


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAPPINGS_PATH = os.path.join(SCRIPT_DIR, "shipping_bypasser_product_color_mappings.json")
_MAPPINGS_LOCK = threading.RLock()
_MAX_PRODUCTS = 2000
_MAX_COLORS_PER_PRODUCT = 500

# Display descriptions of the built-in handlers in crm_shipping_bypasser.py.
# Exact product overrides are separate and take precedence over these rules.
SHIPPING_BRAND_RULES = (
    {
        "brand": "Bella+Canvas",
        "description": (
            "Resolve recognized styles to SanMar's BC prefix, replacing a leading B "
            "or dropping a trailing CRM-only C when needed. Keep letters that belong "
            "to the SanMar style, including CVC."
        ),
        "applies_to": "Only recognized Bella+Canvas style IDs are converted automatically.",
        "examples": [
            {"crm_product_id": "3413C", "sanmar_product_id": "BC3413"},
            {"crm_product_id": "B6500", "sanmar_product_id": "BC6500"},
            {"crm_product_id": "3001CVC", "sanmar_product_id": "BC3001CVC"},
        ],
        "click_inventory_button": True,
    },
    {
        "brand": "Comfort Colors",
        "description": "Remove C letters from the CRM style ID when searching SanMar.",
        "applies_to": "Applies to products identified as Comfort Colors in the product details.",
        "examples": [
            {"crm_product_id": "1467CC", "sanmar_product_id": "1467"},
            {"crm_product_id": "1717CC", "sanmar_product_id": "1717"},
        ],
        "click_inventory_button": False,
    },
    {
        "brand": "A4",
        "description": "Add an a4 prefix to the CRM style when searching SanMar.",
        "applies_to": "Applies to products identified as A4 stock.",
        "examples": [{"crm_product_id": "NW3201", "sanmar_product_id": "a4NW3201"}],
        "click_inventory_button": False,
    },
    {
        "brand": "Gildan",
        "description": (
            "Resolve shortened G-prefixed CRM styles to recognized SanMar styles, "
            "adding zeros where needed. Styles SanMar already lists with G keep that prefix."
        ),
        "applies_to": "Only recognized Gildan style IDs are converted automatically.",
        "examples": [
            {"crm_product_id": "G500", "sanmar_product_id": "5000"},
            {"crm_product_id": "G640", "sanmar_product_id": "64000"},
            {"crm_product_id": "G2400", "sanmar_product_id": "G2400"},
        ],
        "click_inventory_button": False,
    },
    {
        "brand": "Rabbit Skins",
        "description": "Add SanMar's RS prefix when the CRM style omits it.",
        "applies_to": "Only recognized Rabbit Skins style IDs are converted automatically.",
        "examples": [{"crm_product_id": "4400", "sanmar_product_id": "RS4400"}],
        "click_inventory_button": True,
    },
    {
        "brand": "Jerzees",
        "description": (
            "Add a trailing M when that resolves to a recognized SanMar style. "
            "Keep styles that already match SanMar unchanged."
        ),
        "applies_to": "Requires Jerzees in the product details and a recognized style ID.",
        "examples": [
            {"crm_product_id": "562", "sanmar_product_id": "562M"},
            {"crm_product_id": "29LS", "sanmar_product_id": "29LS"},
        ],
        "click_inventory_button": False,
    },
    {
        "brand": "Next Level",
        "description": (
            "Use SanMar's NL prefix. Convert a leading N or trailing NL, "
            "or add NL to a bare numeric style."
        ),
        "applies_to": (
            "N-prefixed, NL-suffixed, and bare numeric conversions require at least four digits. "
            "Bare numeric IDs also require Next Level in the product details."
        ),
        "examples": [
            {"crm_product_id": "N6210", "sanmar_product_id": "NL6210"},
            {"crm_product_id": "3933NL", "sanmar_product_id": "NL3933"},
            {"crm_product_id": "3600", "sanmar_product_id": "NL3600"},
        ],
        "click_inventory_button": False,
    },
)


def mapping_key(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _required_text(value, field_name, context):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} needs a non-empty {field_name}.")
    return value.strip()


def validate_mapping_document(payload):
    if not isinstance(payload, dict):
        raise ValueError("Shipping Bypasser mappings must be a JSON object.")
    products = payload.get("products")
    if not isinstance(products, list):
        raise ValueError("Shipping Bypasser mappings must contain a 'products' list.")
    if len(products) > _MAX_PRODUCTS:
        raise ValueError(f"Shipping Bypasser mappings are limited to {_MAX_PRODUCTS} products.")

    normalized_products = []
    seen_products = set()
    for product_index, item in enumerate(products, start=1):
        context = f"Shipping Bypasser product #{product_index}"
        if not isinstance(item, dict):
            raise ValueError(f"{context} must be an object.")
        crm_product_id = _required_text(item.get("crm_product_id"), "crm_product_id", context)
        sanmar_product_id = _required_text(item.get("sanmar_product_id"), "sanmar_product_id", context)
        product_key = crm_product_id.upper()
        if product_key in seen_products:
            raise ValueError(f"Duplicate Shipping Bypasser crm_product_id: {crm_product_id}")
        seen_products.add(product_key)

        normalized = {
            "crm_product_id": crm_product_id,
            "sanmar_product_id": sanmar_product_id,
        }
        if "sanmar_expected_product_ids" in item:
            expected_ids = item.get("sanmar_expected_product_ids")
            if not isinstance(expected_ids, list) or not expected_ids:
                raise ValueError(f"{context} has an invalid sanmar_expected_product_ids list.")
            normalized["sanmar_expected_product_ids"] = [
                _required_text(value, "SanMar expected product ID", context)
                for value in expected_ids
            ]
        if "click_inventory_button" in item:
            if not isinstance(item.get("click_inventory_button"), bool):
                raise ValueError(f"{context} click_inventory_button must be true or false.")
            normalized["click_inventory_button"] = item["click_inventory_button"]
        if str(item.get("handler") or "").strip():
            normalized["handler"] = str(item["handler"]).strip()

        colors = item.get("colors", [])
        if not isinstance(colors, list):
            raise ValueError(f"{context} colors must be a list.")
        if len(colors) > _MAX_COLORS_PER_PRODUCT:
            raise ValueError(
                f"{context} is limited to {_MAX_COLORS_PER_PRODUCT} color mappings."
            )
        normalized_colors = []
        seen_colors = set()
        for color_index, color in enumerate(colors, start=1):
            color_context = f"Shipping Bypasser color #{color_index} for {crm_product_id}"
            if not isinstance(color, dict):
                raise ValueError(f"{color_context} must be an object.")
            crm_color_id = _required_text(color.get("crm_color_id"), "crm_color_id", color_context)
            sanmar_color_id = _required_text(
                color.get("sanmar_color_id"),
                "sanmar_color_id",
                color_context,
            )
            color_key = mapping_key(crm_color_id)
            if color_key in seen_colors:
                raise ValueError(
                    f"Duplicate Shipping Bypasser crm_color_id for {crm_product_id}: {crm_color_id}"
                )
            seen_colors.add(color_key)
            normalized_colors.append(
                {
                    "crm_color_id": crm_color_id,
                    "sanmar_color_id": sanmar_color_id,
                }
            )
        normalized["colors"] = normalized_colors
        normalized_products.append(normalized)

    document = {}
    for key in ("_instructions", "_example_product"):
        if key in payload:
            document[key] = payload[key]
    document["products"] = normalized_products
    return document


def load_mapping_document(path=MAPPINGS_PATH):
    with _MAPPINGS_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Shipping Bypasser mapping file is missing: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not load Shipping Bypasser mappings from {path}: {exc}") from exc
        try:
            return validate_mapping_document(payload)
        except ValueError as exc:
            raise RuntimeError(f"Invalid Shipping Bypasser mappings in {path}: {exc}") from exc


def build_runtime_indexes(path=MAPPINGS_PATH):
    document = load_mapping_document(path)
    product_overrides = {}
    product_color_aliases = {}
    for item in document["products"]:
        crm_product_id = item["crm_product_id"].upper()
        sanmar_product_id = item["sanmar_product_id"].upper()
        expected_ids = item.get("sanmar_expected_product_ids") or [sanmar_product_id]
        product_overrides[crm_product_id] = {
            "search_id": sanmar_product_id,
            "click_inventory_button": bool(item.get("click_inventory_button", False)),
            "expected_style_keys": [str(value).strip().upper() for value in expected_ids],
            "handler": str(item.get("handler") or crm_product_id).strip(),
        }
        for color in item.get("colors", []):
            product_color_aliases[
                (mapping_key(crm_product_id), mapping_key(color["crm_color_id"]))
            ] = [color["sanmar_color_id"]]
    return product_overrides, product_color_aliases


def save_mapping_products(products, path=MAPPINGS_PATH):
    with _MAPPINGS_LOCK:
        current = load_mapping_document(path)
        candidate = dict(current)
        candidate["products"] = products
        document = validate_mapping_document(candidate)
        directory = os.path.dirname(os.path.abspath(path)) or "."
        temp_path = os.path.join(
            directory,
            f".{os.path.basename(path)}.{os.getpid()}.{threading.get_ident()}.tmp",
        )
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        return document


def mapping_counts(document):
    products = document.get("products", []) if isinstance(document, dict) else []
    return len(products), sum(len(item.get("colors", [])) for item in products if isinstance(item, dict))
