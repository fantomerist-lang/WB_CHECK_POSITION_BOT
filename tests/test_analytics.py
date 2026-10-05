from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

from wb_position_bot.analytics import (
    PositionPoint,
    PositionSeries,
    _font,
    current_week_range,
    load_position_history,
    render_marketplace_legend_chart,
    render_marketplace_report_pdf,
    render_position_chart,
    render_marketplace_overview_chart,
    render_week_position_chart,
    summarize_history,
)
from wb_position_bot.db import active_targets, connect, set_target_active, upsert_target
from wb_position_bot.models import ProductTarget


class AnalyticsTest(unittest.TestCase):
    def test_week_range_starts_on_monday(self):
        tz = ZoneInfo("Europe/Kyiv")
        now = datetime(2026, 5, 28, 12, 0, tzinfo=tz)

        week = current_week_range(tz, now=now)

        self.assertEqual(week.start.date().isoformat(), "2026-05-25")
        self.assertEqual(week.end.date().isoformat(), "2026-06-01")

    def test_loads_week_history_without_deleting_old_weeks(self):
        tz = ZoneInfo("Europe/Kyiv")
        conn = connect(":memory:")
        target = upsert_target(conn, ProductTarget(nm_id=42, search_query="test"))
        conn.executemany(
            """
            insert into position_checks(
              product_id, query, checked_at, own_position, match_reason,
              pages_checked, top_json, own_item_json, warnings_json
            ) values (?, ?, ?, ?, '', 1, '[]', null, '[]')
            """,
            [
                (target.id, "test", "2026-05-20T09:00:00+00:00", 9),
                (target.id, "test", "2026-05-26T09:00:00+00:00", 6),
                (target.id, "test", "2026-05-27T09:00:00+00:00", 4),
            ],
        )
        conn.commit()

        week = current_week_range(tz, now=datetime(2026, 5, 28, 12, 0, tzinfo=tz))
        week_points = load_position_history(conn, target, tz, start=week.start, end=week.end)
        all_points = load_position_history(conn, target, tz)

        self.assertEqual([point.position for point in week_points], [6, 4])
        self.assertEqual([point.position for point in all_points], [9, 6, 4])
        self.assertEqual(summarize_history(all_points).delta, 5)

    def test_can_filter_history_by_auto_checks(self):
        tz = ZoneInfo("Europe/Kyiv")
        conn = connect(":memory:")
        target = upsert_target(conn, ProductTarget(nm_id=42, search_query="test"))
        conn.executemany(
            """
            insert into position_checks(
              product_id, query, checked_at, own_position, match_reason,
              pages_checked, top_json, own_item_json, warnings_json, check_source
            ) values (?, ?, ?, ?, '', 1, '[]', null, '[]', ?)
            """,
            [
                (target.id, "test", "2026-05-26T09:00:00+00:00", 2, "auto"),
                (target.id, "test", "2026-05-26T10:00:00+00:00", 9, "manual"),
                (target.id, "test", "2026-05-27T09:00:00+00:00", 1, "auto"),
            ],
        )
        conn.commit()

        points = load_position_history(conn, target, tz, check_source="auto")

        self.assertEqual([point.position for point in points], [2, 1])

    def test_renders_chart_png(self):
        tz = ZoneInfo("Europe/Kyiv")
        conn = connect(":memory:")
        target = upsert_target(conn, ProductTarget(nm_id=42, search_query="test"))
        conn.execute(
            """
            insert into position_checks(
              product_id, query, checked_at, own_position, match_reason,
              pages_checked, top_json, own_item_json, warnings_json
            ) values (?, ?, ?, ?, '', 1, '[]', null, '[]')
            """,
            (target.id, "test", "2026-05-27T09:00:00+00:00", 4),
        )
        conn.commit()
        points = load_position_history(conn, target, tz)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "chart.png"
            render_position_chart(target, points, output, "WB test", "test")
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 1000)

    def test_renders_week_chart_png(self):
        tz = ZoneInfo("Europe/Kyiv")
        week = current_week_range(tz, now=datetime(2026, 5, 28, 12, 0, tzinfo=tz))
        target = ProductTarget(
            nm_id=399568521,
            name="WB 399568521",
            search_query="1С:Бухгалтерия 8 для Казахстана. Базовая версия",
            own_supplier_name="Кодерлайн",
        )
        points = [
            PositionPoint(datetime(2026, 5, 25, 9, 0, tzinfo=tz), 3, target.search_query),
            PositionPoint(datetime(2026, 5, 26, 9, 0, tzinfo=tz), 2, target.search_query),
            PositionPoint(datetime(2026, 5, 27, 9, 0, tzinfo=tz), 1, target.search_query),
            PositionPoint(datetime(2026, 5, 31, 9, 0, tzinfo=tz), None, target.search_query),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "week-chart.png"
            render_week_position_chart(target, points, output, week, max_search_pages=20)
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 1000)

    def test_renders_marketplace_chart_with_multiple_queries(self):
        tz = ZoneInfo("Europe/Kyiv")
        week = current_week_range(tz, now=datetime(2026, 5, 28, 12, 0, tzinfo=tz))
        targets = [
            ProductTarget(id=1, marketplace="wb", nm_id=42, search_query="бухгалтерия", own_supplier_name="Кодерлайн"),
            ProductTarget(id=2, marketplace="wb", nm_id=43, search_query="программа 1С", own_supplier_name="Кодерлайн"),
        ]
        series = [
            PositionSeries(
                targets[0],
                [
                    PositionPoint(datetime(2026, 5, 25, 9, 0, tzinfo=tz), 3, targets[0].search_query),
                    PositionPoint(datetime(2026, 5, 26, 9, 0, tzinfo=tz), 1, targets[0].search_query),
                ],
            ),
            PositionSeries(
                targets[1],
                [
                    PositionPoint(datetime(2026, 5, 25, 9, 0, tzinfo=tz), 7, targets[1].search_query),
                    PositionPoint(datetime(2026, 5, 26, 9, 0, tzinfo=tz), None, targets[1].search_query),
                ],
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "marketplace-week.png"
            render_marketplace_overview_chart(
                series,
                output,
                marketplace="wb",
                period_title=week.label(),
                x_start=week.start,
                x_end=week.end,
                weekly=True,
            )
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 1000)

            all_time_output = Path(tmp) / "marketplace-all-time.png"
            render_marketplace_overview_chart(
                series,
                all_time_output,
                marketplace="wb",
                period_title="За все время",
            )
            self.assertTrue(all_time_output.exists())
            self.assertGreater(all_time_output.stat().st_size, 1000)

    def test_large_marketplace_chart_keeps_legend_inside_image(self):
        tz = ZoneInfo("Europe/Kyiv")
        week = current_week_range(tz, now=datetime(2026, 5, 28, 12, 0, tzinfo=tz))
        series = []
        for index in range(27):
            target = ProductTarget(
                id=index + 1,
                marketplace="ym",
                external_id=str(1000 + index),
                search_query=f"длинный поисковый запрос {index + 1}",
            )
            series.append(
                PositionSeries(
                    target,
                    [PositionPoint(datetime(2026, 5, 27, 9, 0, tzinfo=tz), index % 8 + 1, target.search_query)],
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "large-marketplace-week.png"
            render_marketplace_overview_chart(
                series,
                output,
                marketplace="ym",
                period_title=week.label(),
                x_start=week.start,
                x_end=week.end,
                weekly=True,
            )
            with Image.open(output) as image:
                self.assertEqual(image.width, 1400)
                self.assertGreaterEqual(image.height, 900)
                self.assertGreater(image.getbbox()[3], 800)

    def test_renders_split_marketplace_legend_and_graph(self):
        target = ProductTarget(id=1, marketplace="ym", external_id="42", search_query="бухгалтерия")
        series = [
            PositionSeries(
                target,
                [PositionPoint(datetime(2026, 5, 27, 9, 0, tzinfo=ZoneInfo("Europe/Kyiv")), 3, target.search_query)],
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            legend = Path(tmp) / "legend.png"
            graph = Path(tmp) / "graph.png"
            render_marketplace_legend_chart(series, legend, "ym", "Неделя тест")
            render_marketplace_overview_chart(series, graph, "ym", "Неделя тест", show_legend=False)

            self.assertTrue(legend.exists())
            self.assertTrue(graph.exists())
            self.assertGreater(legend.stat().st_size, 1000)
            self.assertGreater(graph.stat().st_size, 1000)

    def test_renders_marketplace_pdf_with_a_page_for_each_query(self):
        tz = ZoneInfo("Europe/Kyiv")
        week = current_week_range(tz, now=datetime(2026, 5, 28, 12, 0, tzinfo=tz))
        targets = [
            ProductTarget(id=1, marketplace="ym", external_id="42", search_query="бухгалтерия"),
            ProductTarget(id=2, marketplace="ym", external_id="43", search_query="зарплата"),
        ]
        series = [
            PositionSeries(
                targets[0],
                [PositionPoint(datetime(2026, 5, 26, 9, 0, tzinfo=tz), 3, targets[0].search_query)],
            ),
            PositionSeries(
                targets[1],
                [PositionPoint(datetime(2026, 5, 27, 9, 0, tzinfo=tz), None, targets[1].search_query)],
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "marketplace-report.pdf"
            render_marketplace_report_pdf(
                series,
                output,
                marketplace="ym",
                period_title=f"Неделя {week.label()}",
                max_search_pages=20,
                week_range=week,
            )
            content = output.read_bytes()
            self.assertTrue(content.startswith(b"%PDF"))
            self.assertGreater(len(content), 10_000)

    def test_chart_font_supports_cyrillic(self):
        font = _font(22)

        self.assertIsNotNone(font.getmask("Бухгалтерия Казахстан").getbbox())
        self.assertNotEqual(
            bytes(font.getmask("Бухгалтерия")),
            bytes(font.getmask("??????????")),
        )

    def test_can_disable_target(self):
        conn = connect(":memory:")
        target = upsert_target(conn, ProductTarget(nm_id=42, search_query="test"))

        set_target_active(conn, target.id, False)

        self.assertEqual(active_targets(conn), [])
        self.assertFalse(active_targets(conn, include_inactive=True)[0].active)

    def test_disabling_target_preserves_automatic_history(self):
        tz = ZoneInfo("Europe/Kyiv")
        conn = connect(":memory:")
        target = upsert_target(conn, ProductTarget(nm_id=42, search_query="test"))
        conn.execute(
            """
            insert into position_checks(
              product_id, query, checked_at, own_position, match_reason,
              pages_checked, top_json, own_item_json, warnings_json, check_source
            ) values (?, ?, ?, ?, '', 1, '[]', null, '[]', 'auto')
            """,
            (target.id, "test", "2026-06-10T09:00:00+00:00", 3),
        )
        conn.commit()

        deleted = set_target_active(conn, target.id, False)
        points = load_position_history(conn, deleted, tz, check_source="auto")

        self.assertEqual(active_targets(conn), [])
        self.assertEqual([point.position for point in points], [3])


if __name__ == "__main__":
    unittest.main()
