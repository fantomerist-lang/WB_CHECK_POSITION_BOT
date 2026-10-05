from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .models import ProductTarget


@dataclass(frozen=True)
class PositionPoint:
    checked_at: datetime
    position: int | None
    query: str


@dataclass(frozen=True)
class PositionSeries:
    target: ProductTarget
    points: list[PositionPoint]


@dataclass(frozen=True)
class WeekRange:
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        year, week, _ = self.start.isocalendar()
        return f"{year}-W{week:02d}"

    def label(self) -> str:
        return f"{self.start:%d.%m.%Y} - {(self.end - timedelta(days=1)):%d.%m.%Y}"


@dataclass(frozen=True)
class HistorySummary:
    total_checks: int
    found_checks: int
    missing_checks: int
    best_position: int | None
    worst_position: int | None
    first_position: int | None
    last_position: int | None
    first_seen: datetime | None
    last_seen: datetime | None
    weeks_count: int

    @property
    def delta(self) -> int | None:
        if self.first_position is None or self.last_position is None:
            return None
        return self.first_position - self.last_position


def parse_checked_at(value: str, tz: ZoneInfo) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


def current_week_range(tz: ZoneInfo, now: datetime | None = None) -> WeekRange:
    local_now = now.astimezone(tz) if now else datetime.now(tz)
    start_date = local_now.date() - timedelta(days=local_now.weekday())
    start = datetime.combine(start_date, time.min, tzinfo=tz)
    return WeekRange(start=start, end=start + timedelta(days=7))


def load_position_history(
    conn: sqlite3.Connection,
    target: ProductTarget,
    tz: ZoneInfo,
    start: datetime | None = None,
    end: datetime | None = None,
    check_source: str | None = None,
) -> list[PositionPoint]:
    if not target.id:
        return []
    sql = """
        select checked_at, own_position, query
        from position_checks
        where product_id = ?
        """
    params: list[object] = [target.id]
    if check_source:
        sql += " and check_source = ?"
        params.append(check_source)
    sql += " order by checked_at, id"
    rows = conn.execute(sql, params).fetchall()

    points: list[PositionPoint] = []
    for row in rows:
        checked_at = parse_checked_at(str(row["checked_at"]), tz)
        if start and checked_at < start:
            continue
        if end and checked_at >= end:
            continue
        raw_position = row["own_position"]
        points.append(
            PositionPoint(
                checked_at=checked_at,
                position=int(raw_position) if raw_position is not None else None,
                query=str(row["query"] or ""),
            )
        )
    return points


def summarize_history(points: list[PositionPoint]) -> HistorySummary:
    found = [point for point in points if point.position is not None]
    week_keys = {
        f"{point.checked_at.isocalendar().year}-W{point.checked_at.isocalendar().week:02d}"
        for point in points
    }
    return HistorySummary(
        total_checks=len(points),
        found_checks=len(found),
        missing_checks=len(points) - len(found),
        best_position=min((point.position for point in found), default=None),
        worst_position=max((point.position for point in found), default=None),
        first_position=points[0].position if points else None,
        last_position=points[-1].position if points else None,
        first_seen=points[0].checked_at if points else None,
        last_seen=points[-1].checked_at if points else None,
        weeks_count=len(week_keys),
    )


def position_label(value: int | None) -> str:
    return f"#{value}" if value is not None else "не найдена"


def delta_label(delta: int | None) -> str:
    if delta is None:
        return "-"
    if delta > 0:
        return f"лучше на {delta}"
    if delta < 0:
        return f"хуже на {abs(delta)}"
    return "без изменений"


def format_history_summary(target: ProductTarget, points: list[PositionPoint], title: str) -> str:
    summary = summarize_history(points)
    if not points:
        return f"{title}\n{target.label()}\nПока нет сохраненных проверок."

    lines = [
        title,
        f"Карточка: {target.label()}",
        f"Проверок: {summary.total_checks}",
        f"Найдена: {summary.found_checks}",
        f"Не найдена: {summary.missing_checks}",
        f"Лучшая позиция: {position_label(summary.best_position)}",
        f"Худшая позиция: {position_label(summary.worst_position)}",
        f"Первая позиция: {position_label(summary.first_position)}",
        f"Последняя позиция: {position_label(summary.last_position)}",
        f"Изменение: {delta_label(summary.delta)}",
    ]
    if summary.first_seen and summary.last_seen:
        lines.append(f"Период: {summary.first_seen:%d.%m.%Y} - {summary.last_seen:%d.%m.%Y}")
    if summary.weeks_count:
        lines.append(f"Недель в истории: {summary.weeks_count}")
    return "\n".join(lines)


def format_all_targets_summary(conn: sqlite3.Connection, targets: list[ProductTarget], tz: ZoneInfo) -> str:
    lines = ["Общая статистика маркетплейсов"]
    if not targets:
        return "В базе пока нет карточек."
    for target in targets:
        points = load_position_history(conn, target, tz)
        summary = summarize_history(points)
        lines.append(
            f"\n[{target.marketplace_label()}] {target.product_id() or target.id}: {target.search_query}\n"
            f"Проверок: {summary.total_checks}, "
            f"последняя: {position_label(summary.last_position)}, "
            f"лучшая: {position_label(summary.best_position)}, "
            f"изменение: {delta_label(summary.delta)}"
        )
    return "\n".join(lines)


def render_position_chart(
    target: ProductTarget,
    points: list[PositionPoint],
    output_path: str | Path,
    title: str,
    subtitle: str,
    x_start: datetime | None = None,
    x_end: datetime | None = None,
    width: int = 1200,
    height: int = 700,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    bg = "#f8fafc"
    ink = "#102033"
    muted = "#64748b"
    grid = "#d8e0ea"
    line = "#0f766e"
    point_color = "#0b3b75"
    red = "#c2410c"
    green = "#15803d"

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    font_regular = _font(28)
    font_bold = _font(38, bold=True)
    font_small = _font(22)
    font_tiny = _font(18)

    draw.text((52, 34), title, fill=ink, font=font_bold)
    draw.text((54, 82), subtitle, fill=muted, font=font_small)

    summary = summarize_history(points)
    _draw_pill(draw, (54, 124), f"Последняя {position_label(summary.last_position)}", point_color, font_small)
    _draw_pill(draw, (320, 124), f"Лучшая {position_label(summary.best_position)}", green, font_small)
    _draw_pill(draw, (570, 124), f"Проверок {summary.total_checks}", "#334155", font_small)
    _draw_pill(draw, (810, 124), f"Не найдена {summary.missing_checks}", red, font_small)

    left, top, right, bottom = 88, 205, width - 58, height - 92
    draw.rounded_rectangle((left, top, right, bottom), radius=12, outline="#cbd5e1", width=2, fill="#ffffff")

    if not points:
        message = "Пока нет сохраненных проверок за этот период"
        box = draw.textbbox((0, 0), message, font=font_regular)
        draw.text(
            ((width - (box[2] - box[0])) / 2, (top + bottom) / 2 - 18),
            message,
            fill=muted,
            font=font_regular,
        )
        image.save(output)
        return output

    found_positions = [point.position for point in points if point.position is not None]
    max_position = max(found_positions, default=10)
    y_max = max(max_position + 2, 8)
    missing_y = y_max

    x_min = x_start or points[0].checked_at
    x_max = x_end or points[-1].checked_at
    if x_max <= x_min:
        x_max = x_min + timedelta(hours=1)

    _draw_y_grid(draw, left, top, right, bottom, y_max, grid, muted, font_tiny)
    _draw_x_grid(draw, left, top, right, bottom, x_min, x_max, grid, muted, font_tiny)

    found_xy: list[tuple[float, float]] = []
    for point in points:
        x = _scale_time(point.checked_at, x_min, x_max, left, right)
        if point.position is None:
            y = _scale_position(missing_y, y_max, top, bottom)
            _draw_cross(draw, x, y, red)
            continue
        y = _scale_position(point.position, y_max, top, bottom)
        found_xy.append((x, y))

    if len(found_xy) >= 2:
        draw.line(found_xy, fill=line, width=5, joint="curve")

    for x, y in found_xy:
        draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=point_color, outline="#ffffff", width=3)

    latest = points[-1]
    latest_text = f"Последняя: {position_label(latest.position)} ({latest.checked_at:%d.%m %H:%M})"
    draw.text((left, bottom + 36), latest_text, fill=ink, font=font_small)
    draw.text((right - 410, bottom + 36), "Чем выше точка, тем лучше позиция", fill=muted, font=font_small)

    image.save(output)
    return output


def render_marketplace_report_pdf(
    series: list[PositionSeries],
    output_path: str | Path,
    marketplace: str,
    period_title: str,
    max_search_pages: int,
    week_range: WeekRange | None = None,
) -> Path:
    """Create a readable PDF with one full-size position chart per query.

    The data is read from the same PositionPoint history as the PNG charts. The
    PDF changes presentation only; it never changes or removes stored checks.
    """

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    marketplace_label = "Яндекс Маркет" if marketplace == "ym" else "Wildberries"
    pages = [_render_marketplace_pdf_cover(series, marketplace_label, period_title)]

    with tempfile.TemporaryDirectory(prefix=f"{output.stem}-", dir=output.parent) as temp_dir:
        temp_path = Path(temp_dir)
        for index, item in enumerate(series, start=1):
            chart_path = temp_path / f"query-{index}.png"
            if week_range:
                render_week_position_chart(
                    item.target,
                    item.points,
                    chart_path,
                    week_range,
                    max_search_pages=max_search_pages,
                )
            else:
                render_position_chart(
                    item.target,
                    item.points,
                    chart_path,
                    title=f"Позиция карточки на {marketplace_label}",
                    subtitle=(
                        f"Запрос: {item.target.search_query} | "
                        f"Карточка: {item.target.label()} | {period_title}"
                    ),
                )
            pages.append(
                _render_marketplace_pdf_chart_page(
                    chart_path,
                    marketplace_label,
                    period_title,
                    index,
                    len(series),
                )
            )

    first_page, *remaining_pages = pages
    first_page.save(
        output,
        "PDF",
        save_all=True,
        append_images=remaining_pages,
        resolution=144.0,
    )
    for page in pages:
        page.close()
    return output


def _render_marketplace_pdf_cover(
    series: list[PositionSeries],
    marketplace_label: str,
    period_title: str,
) -> Image.Image:
    width, height = 1200, 848
    image = Image.new("RGB", (width, height), "#f3f7fa")
    draw = ImageDraw.Draw(image)
    ink = "#102033"
    muted = "#64748b"
    green = "#16826a"
    blue = "#1769aa"
    card_border = "#d5e1ea"

    draw.rectangle((0, 0, width, 188), fill=green)
    draw.text((64, 55), "Отчет по позициям", fill="#ffffff", font=_font(44, bold=True))
    draw.text((64, 117), marketplace_label, fill="#ffffff", font=_font(27))
    draw.text((64, 231), period_title, fill=ink, font=_font(27, bold=True))
    draw.text(
        (64, 276),
        "Отдельная страница для каждого поискового запроса",
        fill=muted,
        font=_font(20),
    )

    latest_points = [item.points[-1] for item in series if item.points]
    found_latest = [point for point in latest_points if point.position is not None]
    total_checks = sum(len(item.points) for item in series)
    best_position = min((point.position for point in found_latest), default=None)
    cards = [
        (str(len(series)), "поисковых запросов"),
        (str(len(found_latest)), "найдено в последней проверке"),
        (str(len(latest_points) - len(found_latest)), "не найдено в последней проверке"),
        (position_label(best_position), "лучшая текущая позиция"),
    ]
    x = 64
    for value, label in cards:
        draw.rounded_rectangle((x, 350, x + 250, 512), radius=16, fill="#ffffff", outline=card_border, width=2)
        draw.text((x + 26, 382), value, fill=blue, font=_font(38, bold=True))
        _draw_pdf_wrapped_text(draw, (x + 26, 442), label, 202, _font(17), muted, line_spacing=6)
        x += 273

    draw.text((64, 580), "Как читать отчет", fill=ink, font=_font(28, bold=True))
    notes = [
        "На каждой следующей странице только один запрос и один график, поэтому линии не пересекаются.",
        "Точка означает найденную позицию. Чем ближе к #1, тем лучше результат.",
        "Крестик означает, что карточка не найдена в пределах проверяемой выдачи за этот день.",
        f"В документе {len(series) + 1} страниц: сводка и {len(series)} отдельных графиков. Всего сохранено проверок: {total_checks}.",
    ]
    y = 630
    for note in notes:
        draw.ellipse((70, y + 8, 80, y + 18), fill=green)
        y = _draw_pdf_wrapped_text(draw, (94, y), note, 1020, _font(17), ink, line_spacing=7) + 14

    _draw_pdf_footer(draw, width, height, 1)
    return image


def _render_marketplace_pdf_chart_page(
    chart_path: Path,
    marketplace_label: str,
    period_title: str,
    index: int,
    total: int,
) -> Image.Image:
    width, height = 1200, 848
    image = Image.new("RGB", (width, height), "#f3f7fa")
    draw = ImageDraw.Draw(image)
    draw.text(
        (50, 36),
        f"{marketplace_label}: запрос {index} из {total}",
        fill="#102033",
        font=_font(26, bold=True),
    )
    draw.text((50, 74), period_title, fill="#64748b", font=_font(17))
    with Image.open(chart_path) as chart:
        chart = chart.convert("RGB")
        chart.thumbnail((1100, 642), Image.Resampling.LANCZOS)
        x = (width - chart.width) // 2
        y = 130 + (642 - chart.height) // 2
        image.paste(chart, (x, y))
    _draw_pdf_footer(draw, width, height, index + 1)
    return image


def _draw_pdf_footer(draw: ImageDraw.ImageDraw, width: int, height: int, page_number: int) -> None:
    draw.line((50, height - 42, width - 50, height - 42), fill="#d5e1ea", width=1)
    draw.text((50, height - 29), "Отчет сформирован ботом мониторинга маркетплейсов", fill="#64748b", font=_font(13))
    footer = f"Страница {page_number}"
    footer_box = draw.textbbox((0, 0), footer, font=_font(13))
    draw.text((width - 50 - (footer_box[2] - footer_box[0]), height - 29), footer, fill="#64748b", font=_font(13))


def _draw_pdf_wrapped_text(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    text: str,
    max_width: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    line_spacing: int = 4,
) -> int:
    """Draw text within a fixed width and return the y-coordinate after it."""

    x, y = origin
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    line_height = font.getbbox("Аy")[3] + line_spacing
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += line_height
    return y


def render_week_position_chart(
    target: ProductTarget,
    points: list[PositionPoint],
    output_path: str | Path,
    week_range: WeekRange,
    max_search_pages: int = 20,
    width: int = 1200,
    height: int = 700,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    bg = "#f8fafc"
    ink = "#102033"
    muted = "#64748b"
    grid = "#d8e0ea"
    line = "#0f766e"
    point_color = "#0b3b75"
    red = "#c2410c"
    red_border = "#f97316"

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)

    font_subtitle = _font(22)
    font_label = _font(20, bold=True)
    font_small = _font(18)
    font_tiny = _font(14)
    font_note = _font(13)

    note_box = (842, 70, width - 62, 158)
    left_margin = 54
    supplier = (target.own_supplier_name or target.label()).strip() or "магазина"
    marketplace_name = "Яндекс Маркете" if target.marketplace == "ym" else "WB"
    title = f"Позиция карточки фирмы {supplier} на {marketplace_name} за неделю"
    title_font = _fit_font(draw, title, 38, 26, width - left_margin * 2, bold=True)
    draw.text((left_margin, 28), title, fill=ink, font=title_font)
    draw.text((left_margin, 92), f"Запрос: {target.search_query}", fill=muted, font=font_subtitle)
    draw.text((left_margin, 123), f"Карточка: {target.label()} | {week_range.label()}", fill=muted, font=font_subtitle)

    _draw_explanation_note(
        draw,
        note_box,
        max_search_pages,
        "Яндекс Маркета" if target.marketplace == "ym" else "WB",
        red,
        red_border,
        ink,
        font_small,
        font_note,
        position_limit=target.marketplace == "ym",
    )
    _draw_pill(draw, (54, 160), "точка = позиция", point_color, font_small)
    _draw_pill(draw, (274, 160), "крестик = не найдена", red, font_small)
    draw.text(
        (566, 180),
        "Чем выше точка, тем лучше позиция в поиске",
        fill=muted,
        font=font_small,
        anchor="lm",
    )

    left, top, right, bottom = 92, 235, width - 70, height - 98
    draw.rounded_rectangle((left, top, right, bottom), radius=10, fill="#ffffff", outline="#cbd5e1", width=2)

    day_points = _latest_points_by_weekday(points, week_range)
    found_positions = [point.position for point in day_points if point and point.position is not None]
    y_max = max(8, max(found_positions, default=0))

    _draw_week_grid(draw, left, top, right, bottom, week_range, y_max, grid, muted, font_small, font_tiny)

    if not any(day_points):
        message = "Пока нет автоматических проверок за эту неделю"
        box = draw.textbbox((0, 0), message, font=font_subtitle)
        draw.text(
            ((width - (box[2] - box[0])) / 2, (top + bottom) / 2 - 12),
            message,
            fill=muted,
            font=font_subtitle,
        )
        image.save(output)
        return output

    x_values = [_week_x(index, left, right) for index in range(7)]
    previous_found: tuple[float, float] | None = None
    point_marks: list[tuple[float, float, int]] = []

    for index, point in enumerate(day_points):
        if not point:
            previous_found = None
            continue
        x = x_values[index]
        if point.position is None:
            _draw_missing_marker(draw, x, bottom - 8, width, red, red_border, font_tiny)
            previous_found = None
            continue
        y = _scale_position(point.position, y_max, top, bottom)
        if previous_found:
            draw.line((previous_found[0], previous_found[1], x, y), fill=line, width=4)
        previous_found = (x, y)
        point_marks.append((x, y, point.position))

    for x, y, position in point_marks:
        draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=point_color, outline="#ffffff", width=2)
        _draw_position_tag(draw, x, y, position, point_color, font_label, top, bottom)

    draw.text(
        (left, height - 24),
        "График строится только по автоматическим проверкам. Ручные /check в неделю не попадают.",
        fill=muted,
        font=font_tiny,
    )

    image.save(output)
    return output


SERIES_COLORS = (
    "#0b4f8a",
    "#c2410c",
    "#15803d",
    "#7e22ce",
    "#b91c1c",
    "#0f766e",
    "#a16207",
    "#4338ca",
    "#be185d",
    "#0369a1",
    "#4d7c0f",
    "#9f1239",
)


def _legend_column_count(series_count: int) -> int:
    """Keep a large legend compact while leaving enough width for query names."""
    if series_count <= 4:
        return 1
    if series_count <= 32:
        return 2
    if series_count <= 60:
        return 3
    return 4


def _legend_layout(series_count: int) -> tuple[int, int, int, int, int]:
    columns = _legend_column_count(series_count)
    font_size = 16 if series_count <= 12 else 14 if series_count <= 36 else 12
    rows = max((series_count + columns - 1) // columns, 1)
    row_height = max(22, font_size + 8)
    legend_height = rows * row_height + 18
    label_limit = {1: 62, 2: 54, 3: 34, 4: 25}.get(columns, 22)
    return columns, rows, row_height, legend_height, label_limit


def _draw_marketplace_legend(
    draw: ImageDraw.ImageDraw,
    series: list[PositionSeries],
    width: int,
    legend_top: int,
    ink: str,
    include_latest_position: bool = False,
) -> int:
    columns, rows, row_height, legend_height, label_limit = _legend_layout(len(series))
    legend_font_size = 16 if len(series) <= 12 else 14 if len(series) <= 36 else 12
    legend_font = _font(legend_font_size)
    if include_latest_position and columns == 2:
        label_limit = 40
    column_width = (width - 108) // columns
    for index, item in enumerate(series):
        column = index // rows
        row = index % rows
        x = 56 + column * column_width
        y = legend_top + row * row_height
        color = SERIES_COLORS[index % len(SERIES_COLORS)]
        draw.line((x, y + 10, x + 28, y + 10), fill=color, width=5)
        draw.ellipse((x + 10, y + 5, x + 20, y + 15), fill=color)
        latest = max(item.points, key=lambda point: point.checked_at, default=None)
        latest_text = f"последняя {position_label(latest.position)}" if latest else "нет данных"
        suffix_length = len(latest_text) + 3 if include_latest_position else 0
        query_limit = max(label_limit - suffix_length, 8)
        label = f"{index + 1}. {_ellipsize(item.target.search_query, query_limit)}"
        if include_latest_position:
            label += f" | {latest_text}"
        draw.text((x + 38, y), label, fill=ink, font=legend_font)
    return legend_height


def render_marketplace_legend_chart(
    series: list[PositionSeries],
    output_path: str | Path,
    marketplace: str,
    period_title: str,
    width: int = 1400,
    height: int = 420,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    marketplace_label = "Яндекс Маркет" if marketplace == "ym" else "Wildberries"
    bg = "#f8fafc"
    ink = "#102033"
    muted = "#64748b"
    title_font = _font(38, bold=True)
    subtitle_font = _font(21)
    legend_top = 124
    _, _, _, legend_height, _ = _legend_layout(len(series))
    height = max(height, legend_top + legend_height + 72)

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    draw.text(
        (54, 28),
        f"Легенда: {marketplace_label}",
        fill=ink,
        font=title_font,
    )
    draw.text(
        (56, 82),
        f"{period_title} | Запросов: {len(series)} | Цвет соответствует линии на графике",
        fill=muted,
        font=subtitle_font,
    )
    _draw_marketplace_legend(
        draw,
        series,
        width,
        legend_top,
        ink,
        include_latest_position=True,
    )
    image.save(output)
    return output


def render_marketplace_overview_chart(
    series: list[PositionSeries],
    output_path: str | Path,
    marketplace: str,
    period_title: str,
    x_start: datetime | None = None,
    x_end: datetime | None = None,
    weekly: bool = False,
    max_search_pages: int = 20,
    width: int = 1400,
    height: int = 900,
    show_legend: bool = True,
    show_point_labels: bool = True,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    marketplace_label = "Яндекс Маркет" if marketplace == "ym" else "Wildberries"
    bg = "#f8fafc"
    ink = "#102033"
    muted = "#64748b"
    grid = "#d8e0ea"

    title_font = _font(38, bold=True)
    subtitle_font = _font(21)
    axis_font = _font(16)
    tiny_font = _font(13)
    point_font = _font(14, bold=True)

    legend_top = 124
    _, _, _, legend_height, _ = _legend_layout(len(series)) if show_legend else (0, 0, 0, 0, 0)
    chart_top = legend_top + legend_height + 28 if show_legend else 136
    # Keep the plotting area usable even when the legend grows beyond the default height.
    height = max(height, chart_top + 460)

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)

    draw.text(
        (54, 28),
        f"Позиции всех запросов: {marketplace_label}",
        fill=ink,
        font=title_font,
    )
    draw.text(
        (56, 82),
        f"{period_title} | Запросов: {len(series)} | Только автоматические проверки",
        fill=muted,
        font=subtitle_font,
    )

    if show_legend:
        _draw_marketplace_legend(draw, series, width, legend_top, ink)

    left, right, bottom = 86, width - 54, height - 104
    draw.rounded_rectangle(
        (left, chart_top, right, bottom),
        radius=10,
        fill="#ffffff",
        outline="#cbd5e1",
        width=2,
    )

    all_points = [point for item in series for point in item.points]
    found_positions = [point.position for point in all_points if point.position is not None]
    y_max = max(8, max(found_positions, default=0))

    if weekly and x_start and x_end:
        week_range = WeekRange(x_start, x_end)
        _draw_week_grid(
            draw,
            left,
            chart_top,
            right,
            bottom,
            week_range,
            y_max,
            grid,
            muted,
            axis_font,
            tiny_font,
        )
    else:
        if all_points:
            x_start = x_start or min(point.checked_at for point in all_points)
            x_end = x_end or max(point.checked_at for point in all_points)
        now = datetime.now().astimezone()
        x_start = x_start or now - timedelta(days=6)
        x_end = x_end or now
        if x_end <= x_start:
            x_end = x_start + timedelta(days=1)
        _draw_y_grid(draw, left, chart_top, right, bottom, y_max, grid, muted, axis_font)
        _draw_overview_x_grid(draw, left, chart_top, right, bottom, x_start, x_end, grid, muted, tiny_font)

    if not all_points:
        message = "Пока нет сохраненных автоматических проверок"
        box = draw.textbbox((0, 0), message, font=subtitle_font)
        draw.text(
            ((width - (box[2] - box[0])) / 2, (chart_top + bottom) / 2),
            message,
            fill=muted,
            font=subtitle_font,
        )
    else:
        last_labels: list[tuple[float, float, int, str]] = []
        for index, item in enumerate(series):
            color = SERIES_COLORS[index % len(SERIES_COLORS)]
            # Separate coincident points slightly so many queries checked on the
            # same day and at the same position remain individually visible.
            series_jitter = 0.0
            if len(series) > 4:
                jitter_step = min(4.0, 72.0 / len(series))
                series_jitter = (index - (len(series) - 1) / 2) * jitter_step
            if weekly and x_start and x_end:
                points_by_slot = _latest_points_by_weekday(item.points, WeekRange(x_start, x_end))
                plot_points = [
                    (slot, point)
                    for slot, point in enumerate(points_by_slot)
                    if point is not None
                ]
                x_for_slot = lambda value: _week_x(int(value), left, right)
            else:
                dated = _latest_points_by_date(item.points)
                plot_points = [(point.checked_at, point) for point in dated]
                x_for_slot = lambda value: _scale_time(value, x_start, x_end, left, right)

            previous: tuple[float, float] | None = None
            found_marks: list[tuple[float, float, int]] = []
            for slot, point in plot_points:
                x = x_for_slot(slot) + series_jitter
                x = min(max(x, left + 6), right - 6)
                if point.position is None:
                    offset = ((index % 5) - 2) * 5
                    _draw_colored_cross(draw, x + offset, bottom - 12 - (index // 5) * 10, color)
                    previous = None
                    continue
                y = _scale_position(point.position, y_max, chart_top, bottom)
                if previous:
                    draw.line((previous[0], previous[1], x, y), fill=color, width=4)
                previous = (x, y)
                found_marks.append((x, y, point.position))

            for x, y, position in found_marks:
                draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=color, outline="#ffffff", width=2)
            if show_point_labels and len(series) <= 4:
                for x, y, position in found_marks:
                    label_y = y - 23 if y - 26 > chart_top else y + 12
                    draw.text((x, label_y), f"#{position}", fill=color, font=point_font, anchor="mm")
            elif show_point_labels and found_marks:
                x, y, position = found_marks[-1]
                last_labels.append((x, y, position, color))

        if last_labels:
            _draw_resolved_last_labels(draw, last_labels, point_font, chart_top, bottom)

    limit_text = (
        f"Цвет = поисковый запрос. Крестик = карточка не найдена среди первых "
        f"{max_search_pages} позиций."
        if marketplace == "ym"
        else f"Цвет = поисковый запрос. Крестик = карточка не найдена на первых "
        f"{max_search_pages} страницах."
    )
    draw.text(
        (left, height - 30),
        limit_text,
        fill=muted,
        font=tiny_font,
    )
    image.save(output)
    return output


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    package_dir = Path(__file__).resolve().parent
    project_root = Path(__file__).resolve().parent.parent
    candidates = [
        package_dir / "assets" / "fonts" / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        project_root / "assets" / "fonts" / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        "C:/Windows/Fonts/DejaVuSans-Bold.ttf" if bold else "C:/Windows/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/NotoSans-Bold.ttf" if bold else "C:/Windows/Fonts/NotoSans-Regular.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            font = ImageFont.truetype(str(path), size=size)
            if _font_supports_cyrillic(font):
                return font
    raise RuntimeError(
        "Не найден TTF-шрифт с кириллицей для графика. "
        "Загрузи wb_position_bot/assets/fonts/DejaVuSans.ttf и DejaVuSans-Bold.ttf."
    )


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    start_size: int,
    minimum_size: int,
    max_width: int,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for size in range(start_size, minimum_size - 1, -1):
        candidate = _font(size, bold=bold)
        box = draw.textbbox((0, 0), text, font=candidate)
        if box[2] - box[0] <= max_width:
            return candidate
    return _font(minimum_size, bold=bold)


def _font_supports_cyrillic(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> bool:
    try:
        cyrillic = bytes(font.getmask("Бухгалтерия"))
        fallback = bytes(font.getmask("??????????"))
    except Exception:
        return False
    return bool(cyrillic) and cyrillic != fallback


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    color: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    x, y = xy
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0] + 34
    height = box[3] - box[1] + 22
    draw.rounded_rectangle((x, y, x + width, y + height), radius=18, fill=color)
    draw.text((x + 17, y + 9), text, fill="#ffffff", font=font)


def _scale_time(value: datetime, x_min: datetime, x_max: datetime, left: int, right: int) -> float:
    span = max((x_max - x_min).total_seconds(), 1)
    offset = max((value - x_min).total_seconds(), 0)
    return left + min(offset / span, 1) * (right - left)


def _scale_position(value: int, y_max: int, top: int, bottom: int) -> float:
    ratio = (max(value, 1) - 1) / max(y_max - 1, 1)
    return top + ratio * (bottom - top)


def _draw_y_grid(
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    right: int,
    bottom: int,
    y_max: int,
    grid: str,
    muted: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    ticks = sorted({1, max(2, y_max // 4), max(3, y_max // 2), max(4, (y_max * 3) // 4), y_max})
    for tick in ticks:
        y = _scale_position(tick, y_max, top, bottom)
        draw.line((left, y, right, y), fill=grid, width=1)
        label = f"#{tick}" if tick < y_max else f"#{tick}+"
        draw.text((24, y - 12), label, fill=muted, font=font)


def _draw_x_grid(
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    right: int,
    bottom: int,
    x_min: datetime,
    x_max: datetime,
    grid: str,
    muted: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    span_days = max((x_max - x_min).days, 1)
    if span_days <= 8:
        ticks = [x_min + timedelta(days=index) for index in range(0, min(span_days + 1, 8))]
    else:
        step = max((x_max - x_min) / 5, timedelta(days=1))
        ticks = [x_min + step * index for index in range(6)]

    for tick in ticks:
        x = _scale_time(tick, x_min, x_max, left, right)
        draw.line((x, top, x, bottom), fill=grid, width=1)
        draw.text((x - 35, bottom + 10), tick.strftime("%d.%m"), fill=muted, font=font)


def _draw_explanation_note(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    max_search_pages: int,
    marketplace_name: str,
    red: str,
    border: str,
    ink: str,
    font_title: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_body: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    position_limit: bool = False,
) -> None:
    draw.rounded_rectangle(box, radius=12, fill="#fff8f1", outline=border, width=2)
    center_x = (box[0] + box[2]) / 2
    draw.text((center_x, box[1] + 18), "Не найдена", fill=red, font=font_title, anchor="mm")
    draw.text(
        (center_x, box[1] + 45),
        "Карточка не найдена среди первых" if position_limit else "Карточка не найдена на первых",
        fill=ink,
        font=font_body,
        anchor="mm",
    )
    draw.text(
        (center_x, box[1] + 65),
        (
            f"{max_search_pages} позиций {marketplace_name}"
            if position_limit
            else f"{max_search_pages} страницах {marketplace_name}"
        ),
        fill=ink,
        font=font_body,
        anchor="mm",
    )


def _latest_points_by_weekday(points: list[PositionPoint], week_range: WeekRange) -> list[PositionPoint | None]:
    by_day: list[PositionPoint | None] = [None] * 7
    week_start_date = week_range.start.date()
    week_end_date = week_range.end.date()
    for point in sorted(points, key=lambda item: item.checked_at):
        point_date = point.checked_at.date()
        if point_date < week_start_date or point_date >= week_end_date:
            continue
        index = (point_date - week_start_date).days
        if 0 <= index < 7:
            by_day[index] = point
    return by_day


def _latest_points_by_date(points: list[PositionPoint]) -> list[PositionPoint]:
    by_date: dict[object, PositionPoint] = {}
    for point in sorted(points, key=lambda item: item.checked_at):
        by_date[point.checked_at.date()] = point
    return list(by_date.values())


def _week_x(index: int, left: int, right: int) -> float:
    return left + index * (right - left) / 6


def _draw_week_grid(
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    right: int,
    bottom: int,
    week_range: WeekRange,
    y_max: int,
    grid: str,
    muted: str,
    font_day: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_small: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    day_labels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for index, label in enumerate(day_labels):
        x = _week_x(index, left, right)
        draw.line((x, top, x, bottom), fill=grid, width=1)
        day = week_range.start + timedelta(days=index)
        draw.text((x, bottom + 26), label, fill="#102033", font=font_day, anchor="mm")
        draw.text((x, bottom + 56), day.strftime("%d.%m"), fill=muted, font=font_small, anchor="mm")

    ticks = _week_position_ticks(y_max)
    for tick in ticks:
        y = _scale_position(tick, y_max, top, bottom)
        draw.line((left, y, right, y), fill=grid, width=1)
        label = f"#{tick}+" if tick == y_max and y_max >= 8 else f"#{tick}"
        draw.text((28, y - 1), label, fill=muted, font=font_small, anchor="lm")


def _draw_overview_x_grid(
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    right: int,
    bottom: int,
    x_min: datetime,
    x_max: datetime,
    grid: str,
    muted: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    span = x_max - x_min
    span_days = max(span.total_seconds() / 86400, 1)
    tick_count = min(7, max(2, int(span_days) + 1))
    for index in range(tick_count):
        ratio = index / max(tick_count - 1, 1)
        tick = x_min + span * ratio
        x = left + ratio * (right - left)
        draw.line((x, top, x, bottom), fill=grid, width=1)
        draw.text((x, bottom + 25), tick.strftime("%d.%m.%y"), fill=muted, font=font, anchor="mm")


def _week_position_ticks(y_max: int) -> list[int]:
    if y_max <= 8:
        return list(range(1, 9))
    if y_max <= 12:
        return list(range(1, y_max + 1))
    ticks = {1, 2, 3, max(4, y_max // 4), max(5, y_max // 2), max(6, (y_max * 3) // 4), y_max}
    return sorted(ticks)


def _draw_missing_marker(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    image_width: int,
    red: str,
    border: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    center_x = min(max(x, 74), image_width - 74)
    bubble = (center_x - 70, y - 48, center_x + 70, y - 6)
    draw.rounded_rectangle(bubble, radius=19, fill="#fff1ea", outline=border, width=2)
    draw.text((center_x, y - 34), "не найдена", fill=red, font=font, anchor="mm")
    draw.line((center_x - 12, y - 20, center_x + 12, y + 4), fill=red, width=3)
    draw.line((center_x - 12, y + 4, center_x + 12, y - 20), fill=red, width=3)


def _draw_position_tag(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    position: int,
    color: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    top: int,
    bottom: int,
) -> None:
    offset_y = -24
    if y - 34 < top:
        offset_y = 28
    if y + 34 > bottom:
        offset_y = -28
    draw.text((x, y + offset_y), f"#{position}", fill=color, font=font, anchor="mm")


def _draw_colored_cross(draw: ImageDraw.ImageDraw, x: float, y: float, color: str) -> None:
    size = 7
    draw.line((x - size, y - size, x + size, y + size), fill=color, width=3)
    draw.line((x - size, y + size, x + size, y - size), fill=color, width=3)


def _draw_resolved_last_labels(
    draw: ImageDraw.ImageDraw,
    labels: list[tuple[float, float, int, str]],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    top: int,
    bottom: int,
) -> None:
    ordered = sorted(labels, key=lambda item: item[1])
    positions: list[float] = []
    # Leave breathing room so a dense stack never touches the plot border.
    first_label_y = top + 30
    last_label_y = bottom - 30
    available_height = max(last_label_y - first_label_y, 1)
    minimum_gap = min(19, available_height / max(len(ordered) - 1, 1))
    for _, y, _, _ in ordered:
        label_y = max(y, first_label_y)
        if positions:
            label_y = max(label_y, positions[-1] + minimum_gap)
        positions.append(label_y)
    if positions and positions[-1] > last_label_y:
        positions = [first_label_y + index * minimum_gap for index in range(len(ordered))]

    for (x, _, position, color), label_y in zip(ordered, positions):
        draw.text(
            (x - 12, label_y),
            f"#{position}",
            fill=color,
            font=font,
            anchor="rm",
            stroke_width=3,
            stroke_fill="#ffffff",
        )


def _ellipsize(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def _draw_cross(draw: ImageDraw.ImageDraw, x: float, y: float, color: str) -> None:
    size = 9
    draw.line((x - size, y - size, x + size, y + size), fill=color, width=4)
    draw.line((x - size, y + size, x + size, y - size), fill=color, width=4)
