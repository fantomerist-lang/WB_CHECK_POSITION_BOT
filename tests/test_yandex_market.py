from __future__ import annotations

import unittest
import sqlite3

from wb_position_bot.analyzer import analyze_target
from wb_position_bot.db import connect, get_target_by_external_id, get_target_by_nm_id, migrate, upsert_target
from wb_position_bot.models import ProductTarget
from wb_position_bot.target_parser import extract_yandex_product_id, parse_add_args
from wb_position_bot.yandex_market import parse_yandex_search_html


SEARCH_HTML = r'''
<html><body>
<a href="/card/telefon-test/103705469335?do-waremd5=offer-one">Телефон</a>
<script>
{"widget":{"oskuId":103705469335,"offerId":"offer-one"}}
{"pendingCartItem":{"productId":1172957405,"skuId":"103691210179","offerId":"offer-one","businessId":157398429,"name":"Телефон Test 64 ГБ","price":4545}}
{"businessId":157398429,"businessName":"Магазин Тест"}
{"widget":{"oskuId":998877665544,"offerId":"offer-two"}}
{"pendingCartItem":{"productId":998877,"skuId":"99887711","offerId":"offer-two","businessId":924574,"name":"Второй телефон","price":{"value":7990}}}
{"businessId":924574,"shopName":"Второй продавец"}
</script>
</body></html>
'''


class StaticClient:
    def __init__(self, items):
        self.items = items

    def search(self, query: str, page: int = 1):
        return self.items if page == 1 else []


class YandexMarketTest(unittest.TestCase):
    def test_parses_search_cards_and_sellers(self):
        items = parse_yandex_search_html(SEARCH_HTML)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].external_id, "103705469335")
        self.assertEqual(items[0].nm_id, 1172957405)
        self.assertEqual(items[0].supplier_name, "Магазин Тест")
        self.assertEqual(items[0].price, 4545)
        self.assertIn("/card/telefon-test/103705469335", items[0].url)
        self.assertEqual(items[1].supplier_name, "Второй продавец")
        self.assertEqual(items[1].price, 7990)

    def test_analyzer_matches_yandex_external_id(self):
        items = parse_yandex_search_html(SEARCH_HTML)
        target = ProductTarget(
            marketplace="ym",
            external_id="998877665544",
            search_query="телефон",
            own_supplier_name="Второй продавец",
        )

        analysis = analyze_target(target, StaticClient(items), max_pages=2)

        self.assertEqual(analysis.own_position, 2)
        self.assertEqual(analysis.match_reason, "external_id")

    def test_yandex_add_accepts_id_or_card_url(self):
        direct = parse_add_args("/addym 103705469335 | телефон | Магазин", marketplace="ym")
        linked = parse_add_args(
            "/addym https://market.yandex.ru/card/telefon/103705469335 | телефон | Магазин",
            marketplace="ym",
        )

        self.assertEqual(direct.external_id, "103705469335")
        self.assertEqual(linked.external_id, "103705469335")
        self.assertEqual(extract_yandex_product_id(linked.name), "103705469335")

    def test_old_wb_add_format_stays_supported(self):
        target = parse_add_args("/add бухгалтерия | Кодерлайн", marketplace="wb")

        self.assertEqual(target.marketplace, "wb")
        self.assertIsNone(target.nm_id)
        self.assertEqual(target.search_query, "бухгалтерия")

    def test_database_keeps_marketplaces_separate(self):
        conn = connect(":memory:")
        wb = upsert_target(conn, ProductTarget(marketplace="wb", nm_id=42, search_query="test"))
        ym = upsert_target(
            conn,
            ProductTarget(marketplace="ym", external_id="42", search_query="test", own_supplier_name="shop"),
        )

        self.assertNotEqual(wb.id, ym.id)
        self.assertEqual(get_target_by_nm_id(conn, 42).marketplace, "wb")
        self.assertEqual(get_target_by_external_id(conn, "ym", "42").marketplace, "ym")

    def test_migrates_existing_wb_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            create table tracked_products (
              id integer primary key autoincrement,
              nm_id integer,
              sku text not null default '',
              name text not null default '',
              search_query text not null,
              own_supplier_id integer,
              own_supplier_name text not null default '',
              note text not null default '',
              active integer not null default 1,
              created_at text not null default current_timestamp,
              updated_at text not null default current_timestamp
            );
            create unique index idx_tracked_products_nm_id on tracked_products(nm_id) where nm_id is not null;
            insert into tracked_products(nm_id, search_query) values(399568521, 'test');
            """
        )

        migrate(conn)
        row = conn.execute("select marketplace, external_id from tracked_products").fetchone()

        self.assertEqual(row["marketplace"], "wb")
        self.assertEqual(row["external_id"], "399568521")


if __name__ == "__main__":
    unittest.main()
