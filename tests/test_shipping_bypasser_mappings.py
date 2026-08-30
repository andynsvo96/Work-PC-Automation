import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server
import shipping_bypasser_mappings


class ShippingBypasserMappingStorageTests(unittest.TestCase):
    def _document(self):
        return {
            "_instructions": ["keep me"],
            "_example_product": {"crm_product_id": "EXAMPLE"},
            "products": [
                {
                    "crm_product_id": "CRM100",
                    "sanmar_product_id": "SM100",
                    "colors": [
                        {
                            "crm_color_id": "Blue / White",
                            "sanmar_color_id": "Blue/ White",
                        }
                    ],
                }
            ],
        }

    def test_save_preserves_help_metadata_and_writes_validated_products(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mappings.json"
            path.write_text(json.dumps(self._document()), encoding="utf-8")
            products = [
                {
                    "crm_product_id": "CRM200",
                    "sanmar_product_id": "SM200",
                    "colors": [],
                }
            ]

            saved = shipping_bypasser_mappings.save_mapping_products(products, str(path))
            on_disk = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["_instructions"], ["keep me"])
        self.assertIn("_example_product", saved)
        self.assertEqual(on_disk["products"], products)

    def test_duplicate_normalized_color_ids_are_rejected(self):
        document = self._document()
        document["products"][0]["colors"].append(
            {"crm_color_id": "BLUE-WHITE", "sanmar_color_id": "Other"}
        )

        with self.assertRaisesRegex(ValueError, "Duplicate.*crm_color_id"):
            shipping_bypasser_mappings.validate_mapping_document(document)


class ShippingBypasserMappingApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_pin_required = server.APP_PIN_REQUIRED
        server.APP_PIN_REQUIRED = False
        self.client = server.app.test_client()

    def tearDown(self):
        server.APP_PIN_REQUIRED = self.previous_pin_required

    def test_get_returns_editable_products(self):
        document = {"products": [{"crm_product_id": "A", "sanmar_product_id": "B", "colors": []}]}
        with mock.patch.object(server, "load_mapping_document", return_value=document):
            response = self.client.get("/api/shipping-bypasser-mappings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["products"], document["products"])

    def test_post_returns_validation_error(self):
        with mock.patch.object(server, "save_mapping_products", side_effect=ValueError("Duplicate product")):
            response = self.client.post(
                "/api/shipping-bypasser-mappings",
                json={"products": []},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Duplicate product", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
