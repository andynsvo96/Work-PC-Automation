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
