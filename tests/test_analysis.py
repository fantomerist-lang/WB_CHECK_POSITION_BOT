from __future__ import annotations

import unittest

from wb_position_bot.analyzer import analyze_target
from wb_position_bot.models import ProductTarget, SearchResultItem


class FakeClient:
    def __init__(self, pages):
        self.pages = pages

    def search(self, query: str, page: int = 1):
        return self.pages.get(page, [])


def item(nm_id: int, supplier: str, supplier_id: int | None = None) -> SearchResultItem:
    return SearchResultItem(
        rank=0,
        nm_id=nm_id,
        name=f"Product {nm_id}",
        supplier_id=supplier_id,
        supplier_name=supplier,
        url=f"https://example.test/{nm_id}",
    )


class AnalysisTest(unittest.TestCase):
    def test_finds_own_card_after_top_5(self):
        target = ProductTarget(nm_id=42, search_query="test")
        client = FakeClient(
            {
                1: [
                    item(1, "Shop 1"),
                    item(2, "Shop 2"),
                    item(3, "Shop 3"),
                    item(4, "Shop 4"),
                    item(5, "Shop 5"),
                    item(6, "Shop 6"),
                    item(42, "Own shop"),
                ]
            }
        )

        analysis = analyze_target(target, client, max_pages=3)

        self.assertEqual(analysis.own_position, 7)
        self.assertEqual(len(analysis.top_items), 5)
        self.assertEqual([i.nm_id for i in analysis.top_items], [1, 2, 3, 4, 5])

    def test_can_match_by_supplier_name(self):
        target = ProductTarget(search_query="test", own_supplier_name="My Shop")
        client = FakeClient({1: [item(10, "Other"), item(11, "My Shop")]})

        analysis = analyze_target(target, client, max_pages=1)

        self.assertEqual(analysis.own_position, 2)
        self.assertEqual(analysis.match_reason, "supplier_name")
        self.assertTrue(analysis.warnings)


if __name__ == "__main__":
    unittest.main()
