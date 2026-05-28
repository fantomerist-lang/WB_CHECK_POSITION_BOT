from __future__ import annotations

import unittest

from wb_position_bot.wildberries import (
    SEARCH_ENDPOINTS,
    WildberriesClient,
    WildberriesError,
    extract_products,
    parse_json_response,
    parse_search_item,
    timeout_or_network_error_message,
)


class WildberriesParsingTest(unittest.TestCase):
    def test_parses_first_json_object_from_wrapped_response(self):
        payload = parse_json_response('noise {"data": {"products": [{"id": 1, "name": "Test"}]}} trailing')

        self.assertEqual(payload["data"]["products"][0]["id"], 1)

    def test_parses_json_with_control_char_inside_string(self):
        payload = parse_json_response('{"metadata": {"catalog_value": "a\x01b"}, "data": {"products": []}}')

        self.assertEqual(payload["metadata"]["catalog_value"], "a\x01b")

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

    def test_remote_close_message_suggests_retries(self):
        message = timeout_or_network_error_message(
            ConnectionError("Remote end closed connection without response"),
            60,
        )

        self.assertIn("WB_REQUEST_RETRIES=4", message)

    def test_not_found_metadata_is_endpoint_error(self):
        with self.assertRaises(WildberriesError):
            parse_json_response('{"metadata":{"preset_normquery_map":{"202812737":"8 в 1"}},ot Found')

    def test_keeps_old_search_endpoint_as_fallback(self):
        self.assertIn("https://search.wb.ru/exactmatch/ru/common/v4/search", SEARCH_ENDPOINTS)

    def test_search_skips_empty_endpoint_response(self):
        class FakeClient(WildberriesClient):
            def __init__(self):
                super().__init__(retries=1)
                self.calls = 0

            def _warm_up(self):
                return None

            def _get_json(self, endpoint, params):
                self.calls += 1
                if self.calls == 1:
                    return {"metadata": {"catalog_type": "preset"}}
                return {"data": {"products": [{"id": 789, "name": "Product", "supplier": "Shop"}]}}

        result = FakeClient().search("test")

        self.assertEqual([item.nm_id for item in result], [789])


if __name__ == "__main__":
    unittest.main()
