from __future__ import annotations

import unittest
import sqlite3
import os
import time
from unittest.mock import patch

from wb_position_bot.analyzer import analyze_target
from wb_position_bot.config import get_config
from wb_position_bot.db import (
    active_authorized_users,
    active_targets,
    authorize_user,
    claim_unowned_targets,
    consume_member_invite,
    connect,
    create_member_invite,
    get_authorized_user,
    get_target_by_external_id,
    get_target_by_nm_id,
    migrate,
    revoke_user,
    transfer_targets_between_owners,
    upsert_target,
)
from wb_position_bot.models import ProductTarget
from wb_position_bot.target_parser import extract_yandex_product_id, parse_add_args
from wb_position_bot.yandex_market import (
    YandexMarketClient,
    YandexMarketError,
    parse_apify_search_items,
    parse_yandex_search_html,
)


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


APIFY_ROWS = [
    {
        "searchPosition": 2,
        "title": "Вторая карточка",
        "modelId": 2002,
        "marketSku": "900000002",
        "oskuId": 4717385177,
        "articleNumber": 4717385177,
        "sellerName": "Кодерлайн",
        "businessId": 77,
        "price": 2590,
        "rating": 4.9,
        "reviewCount": 12,
        "productUrl": "https://market.yandex.ru/card/programma/4717385177",
    },
    {
        "searchPosition": 1,
        "title": "Первая карточка",
        "modelId": 1001,
        "marketSku": "900000001",
        "oskuId": 1111111111,
        "sellerName": "Другой магазин",
        "businessId": 88,
        "price": 2999,
        "productUrl": "https://market.yandex.ru/card/programma/1111111111",
    },
]


class StaticClient:
    def __init__(self, items):
        self.items = items

    def search(self, query: str, page: int = 1):
        return self.items if page == 1 else []


class YandexMarketTest(unittest.TestCase):
    def test_apify_configuration_uses_one_batched_search(self):
        with patch.dict(
            os.environ,
            {
                "APIFY_API_TOKEN": "apify-test-key",
                "YM_MAX_SEARCH_PAGES": "20",
                "YM_APIFY_MAX_ITEMS": "50",
            },
            clear=True,
        ):
            config = get_config(require_telegram=False)

        self.assertEqual(config.apify_api_token, "apify-test-key")
        self.assertEqual(config.ym_max_search_pages, 1)
        self.assertEqual(config.ym_apify_max_items, 50)
        self.assertEqual(config.ym_check_timeout, 300.0)

    def test_parses_apify_rows_in_search_order_and_keeps_all_ids(self):
        items = parse_apify_search_items(APIFY_ROWS)

        self.assertEqual([item.name for item in items], ["Первая карточка", "Вторая карточка"])
        self.assertEqual(items[1].external_id, "4717385177")
        self.assertIn("900000002", items[1].alternate_ids)
        self.assertEqual(items[1].supplier_name, "Кодерлайн")
        self.assertEqual(items[1].price, 2590)

    def test_apify_search_is_cached_and_reports_position_limit(self):
        client = YandexMarketClient(
            apify_api_token="apify-test-key",
            apify_max_items=50,
        )
        target = ProductTarget(
            marketplace="ym",
            external_id="4717385177",
            search_query="1с бухгалтерия базовая",
        )
        with patch.object(client, "_post_apify_json", return_value=APIFY_ROWS) as request:
            analysis = analyze_target(target, client, max_pages=1)
            client.search(target.search_query, page=1)

        self.assertEqual(request.call_count, 1)
        self.assertEqual(analysis.own_position, 2)
        self.assertEqual(analysis.search_scope, "среди первых 2 позиций выдачи")

    def test_yandex_does_not_inherit_wildberries_proxy(self):
        with patch.dict(
            os.environ,
            {
                "WB_PROXY_URL": "https://unblock.example:60000",
                "WB_PROXY_AUTH_TOKEN": "wb-token",
            },
            clear=True,
        ):
            config = get_config(require_telegram=False)

        self.assertEqual(config.ym_proxy_url, "")
        self.assertEqual(config.ym_proxy_auth_token, "")
        self.assertEqual(config.ym_request_timeout, 20.0)
        self.assertEqual(config.ym_check_timeout, 120.0)
        self.assertFalse(config.ym_enrich_sellers)

    def test_yandex_operation_deadline_stops_long_check(self):
        client = YandexMarketClient(timeout=5, retries=1, enrich_sellers=False)
        client.start_operation(0.01)
        time.sleep(0.02)

        with self.assertRaisesRegex(YandexMarketError, "превысила допустимое время"):
            client._remaining_time()

        client.finish_operation()

    def test_reef_configuration_caps_wb_search_at_three_pages(self):
        with patch.dict(
            os.environ,
            {
                "REEF_API_KEY": "reef-test-key",
                "WB_MAX_SEARCH_PAGES": "20",
            },
            clear=True,
        ):
            config = get_config(require_telegram=False)

        self.assertEqual(config.reef_api_key, "reef-test-key")
        self.assertEqual(config.reef_country, "ru")
        self.assertEqual(config.wb_max_search_pages, 3)

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

    def test_same_wb_card_can_track_multiple_queries(self):
        conn = connect(":memory:")
        first = upsert_target(
            conn,
            ProductTarget(
                marketplace="wb",
                external_id="42",
                nm_id=42,
                sku="42",
                search_query="бухгалтерия",
                own_supplier_name="Кодерлайн",
            ),
        )
        second = upsert_target(
            conn,
            ProductTarget(
                marketplace="wb",
                external_id="42",
                nm_id=42,
                sku="42",
                search_query="1С для Казахстана",
                own_supplier_name="Кодерлайн",
            ),
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(conn.execute("select count(*) from tracked_products").fetchone()[0], 2)

    def test_same_yandex_card_can_track_multiple_queries(self):
        conn = connect(":memory:")
        first = upsert_target(
            conn,
            ProductTarget(
                marketplace="ym",
                external_id="998877",
                sku="998877",
                search_query="бухгалтерия",
                own_supplier_name="Кодерлайн",
            ),
        )
        second = upsert_target(
            conn,
            ProductTarget(
                marketplace="ym",
                external_id="998877",
                sku="998877",
                search_query="1С для Казахстана",
                own_supplier_name="Кодерлайн",
            ),
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(conn.execute("select count(*) from tracked_products").fetchone()[0], 2)

    def test_readding_same_card_and_query_updates_existing_row(self):
        conn = connect(":memory:")
        first = upsert_target(
            conn,
            ProductTarget(
                marketplace="wb",
                external_id="42",
                nm_id=42,
                sku="42",
                search_query="бухгалтерия",
                own_supplier_name="Старое имя",
            ),
        )
        updated = upsert_target(
            conn,
            ProductTarget(
                marketplace="wb",
                external_id="42",
                nm_id=42,
                sku="42",
                search_query="бухгалтерия",
                own_supplier_name="Кодерлайн",
            ),
        )

        self.assertEqual(first.id, updated.id)
        self.assertEqual(updated.own_supplier_name, "Кодерлайн")
        self.assertEqual(conn.execute("select count(*) from tracked_products").fetchone()[0], 1)

    def test_same_card_and_query_are_separate_for_each_user(self):
        conn = connect(":memory:")
        first = upsert_target(
            conn,
            ProductTarget(owner_chat_id=101, marketplace="wb", external_id="42", nm_id=42, search_query="test"),
        )
        second = upsert_target(
            conn,
            ProductTarget(owner_chat_id=202, marketplace="wb", external_id="42", nm_id=42, search_query="test"),
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual([item.id for item in active_targets(conn, owner_chat_id=101)], [first.id])
        self.assertEqual([item.id for item in active_targets(conn, owner_chat_id=202)], [second.id])

    def test_transfer_preserves_target_ids_and_position_history(self):
        conn = connect(":memory:")
        first = upsert_target(
            conn,
            ProductTarget(owner_chat_id=202, marketplace="wb", external_id="42", nm_id=42, search_query="первый"),
        )
        second = upsert_target(
            conn,
            ProductTarget(
                owner_chat_id=202,
                marketplace="ym",
                external_id="103705469335",
                search_query="второй",
                active=False,
            ),
        )
        admin_target = upsert_target(
            conn,
            ProductTarget(owner_chat_id=101, marketplace="wb", external_id="99", nm_id=99, search_query="админ"),
        )
        conn.execute(
            "insert into position_checks(product_id, query, checked_at, top_json) values (?, ?, ?, ?)",
            (first.id, "первый", "2026-10-07T09:00:00+03:00", "[]"),
        )
        conn.commit()

        moved = transfer_targets_between_owners(conn, 202, 303)

        self.assertEqual(moved, 2)
        self.assertEqual(active_targets(conn, include_inactive=True, owner_chat_id=202), [])
        self.assertEqual(
            [target.id for target in active_targets(conn, include_inactive=True, owner_chat_id=303)],
            [first.id, second.id],
        )
        self.assertEqual(conn.execute("select product_id from position_checks").fetchone()[0], first.id)
        self.assertEqual(active_targets(conn, include_inactive=True, owner_chat_id=101)[0].id, admin_target.id)

    def test_transfer_rejects_destination_with_existing_targets(self):
        conn = connect(":memory:")
        upsert_target(conn, ProductTarget(owner_chat_id=202, marketplace="wb", nm_id=42, search_query="источник"))
        upsert_target(conn, ProductTarget(owner_chat_id=303, marketplace="wb", nm_id=43, search_query="получатель"))

        with self.assertRaisesRegex(ValueError, "У получателя уже есть запросы"):
            transfer_targets_between_owners(conn, 202, 303)

    def test_authorized_user_and_legacy_target_migration(self):
        conn = connect(":memory:")
        legacy = upsert_target(conn, ProductTarget(nm_id=42, search_query="test"))
        authorize_user(conn, 202, username="member", display_name="Member")
        claim_unowned_targets(conn, 101)

        self.assertEqual(active_targets(conn, owner_chat_id=101)[0].id, legacy.id)
        self.assertEqual(get_authorized_user(conn, 202)["username"], "member")
        self.assertEqual(len(active_authorized_users(conn)), 1)
        self.assertTrue(revoke_user(conn, 202))
        self.assertIsNone(get_authorized_user(conn, 202))

    def test_multiple_invites_are_independent_and_never_expire(self):
        conn = connect(":memory:")
        create_member_invite(conn, "first-invite")
        create_member_invite(conn, "second-invite")

        self.assertTrue(consume_member_invite(conn, "first-invite", 202))
        self.assertFalse(consume_member_invite(conn, "first-invite", 303))
        self.assertTrue(consume_member_invite(conn, "second-invite", 303))

    def test_invite_migration_preserves_existing_marketplace_data(self):
        conn = connect(":memory:")
        target = upsert_target(
            conn,
            ProductTarget(owner_chat_id=101, marketplace="wb", external_id="42", nm_id=42, search_query="test"),
        )
        authorize_user(conn, 202, username="member", display_name="Member")
        conn.execute(
            "insert into position_checks(product_id, query, checked_at, top_json) values (?, ?, ?, ?)",
            (target.id, "test", "2026-09-30T09:00:00+03:00", "[]"),
        )
        conn.commit()

        migrate(conn)

        self.assertEqual(conn.execute("select count(*) from tracked_products").fetchone()[0], 1)
        self.assertEqual(conn.execute("select count(*) from position_checks").fetchone()[0], 1)
        self.assertEqual(len(active_authorized_users(conn)), 1)
        self.assertEqual(conn.execute("select count(*) from member_invites").fetchone()[0], 0)

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
