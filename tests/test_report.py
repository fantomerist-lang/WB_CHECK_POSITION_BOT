from __future__ import annotations

import unittest

from wb_position_bot.models import PositionAnalysis, ProductTarget, SearchResultItem
from wb_position_bot.report import format_full_report_messages, format_short_summary


def item(rank: int, nm_id: int, name: str, supplier: str, price: float | None = None) -> SearchResultItem:
    return SearchResultItem(
        rank=rank,
        nm_id=nm_id,
        name=name,
        supplier_name=supplier,
        sale_price=price,
        url=f"https://example.test/{nm_id}",
    )


class ReportTest(unittest.TestCase):
    def test_short_summary_includes_numbered_top_items(self):
        analysis = PositionAnalysis(
            target=ProductTarget(id=1, nm_id=42, search_query="query"),
            query="query",
            checked_at="2026-05-29T09:00:00+00:00",
            top_items=[
                item(1, 42, "Own product", "Own shop", 100),
                item(2, 43, "Competitor product", "Other shop", 200),
            ],
            own_item=item(1, 42, "Own product", "Own shop", 100),
            own_position=1,
            match_reason="nm_id",
            pages_checked=1,
            warnings=[],
        )

        text = format_short_summary([analysis])

        self.assertIn("Топ-5 выдачи:", text)
        self.assertIn("1. Own product - Own shop - 100 ₽", text)
        self.assertIn("2. Competitor product - Other shop - 200 ₽", text)
        self.assertNotIn("Топ-5 магазинов:", text)

    def test_full_report_messages_include_check_details(self):
        analysis = PositionAnalysis(
            target=ProductTarget(id=1, nm_id=42, search_query="query"),
            query="query",
            checked_at="2026-05-29T09:00:00+00:00",
            top_items=[
                item(1, 42, "Own product", "Own shop", 100),
                item(2, 43, "Competitor product", "Other shop", 200),
            ],
            own_item=item(1, 42, "Own product", "Own shop", 100),
            own_position=1,
            match_reason="nm_id",
            pages_checked=1,
            warnings=[],
        )

        messages = format_full_report_messages([analysis])

        self.assertEqual(messages[0], "Отчет WB по позициям\nПроверено запросов: 1")
        self.assertIn("Запрос 1/1", messages[1])
        self.assertIn("Запрос: query", messages[1])
        self.assertIn("Позиция твоей карточки: #1 в топ-5", messages[1])
        self.assertIn("Топ-5 выдачи:", messages[1])
        self.assertIn("https://example.test/42", messages[1])


if __name__ == "__main__":
    unittest.main()
