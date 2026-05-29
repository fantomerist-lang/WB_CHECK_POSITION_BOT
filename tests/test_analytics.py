from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from wb_position_bot.analytics import (
    _font,
    current_week_range,
    load_position_history,
    render_position_chart,
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


if __name__ == "__main__":
    unittest.main()
