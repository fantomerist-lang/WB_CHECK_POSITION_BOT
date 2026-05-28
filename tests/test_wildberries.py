from __future__ import annotations

import unittest

from wb_position_bot.wildberries import extract_products, parse_json_response, parse_search_item


class WildberriesParsingTest(unittest.TestCase):
    def test_parses_first_json_object_from_wrapped_response(self):
        payload = parse_json_response('noise {"data": {"products": [{"id": 1, "name": "Test"}]}} trailing')

        self.assertEqual(payload["data"]["products"][0]["id"], 1)

    def test_extracts_nested_cards_with_nm_id(self):
        payload = {
            "metadata": {"catalog_type": "preset"},
            "result": {
                "cards": [
                    {"nmId": 123, "name": "1C", "supplierName": "Coderline"},
                ]
            },
        }

        products = extract_products(payload)
        parsed = parse_search_item(products[0])

        self.assertEqual(parsed.nm_id, 123)
        self.assertEqual(parsed.supplier_name, "Coderline")

    def test_extracts_price_from_wb_sizes_shape(self):
        parsed = parse_search_item(
            {
                "nm": 456,
                "name": "Product",
                "supplier": "Shop",
                "sizes": [{"price": {"basic": 123400, "total": 99900}}],
            }
        )

        self.assertEqual(parsed.price, 1234)
        self.assertEqual(parsed.sale_price, 999)


if __name__ == "__main__":
    unittest.main()
