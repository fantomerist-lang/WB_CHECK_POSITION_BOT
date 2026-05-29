from __future__ import annotations

import sqlite3
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
    lines = ["Общая статистика WB"]
    if not targets:
        return "В базе пока нет карточек."
    for target in targets:
        points = load_position_history(conn, target, tz)
        summary = summarize_history(points)
        lines.append(
            f"\n{target.nm_id or target.id}: {target.search_query}\n"
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
    _draw_pill(draw, (54, 124), f"Last {position_label(summary.last_position)}", point_color, font_small)
    _draw_pill(draw, (260, 124), f"Best {position_label(summary.best_position)}", green, font_small)
    _draw_pill(draw, (460, 124), f"Checks {summary.total_checks}", "#334155", font_small)
    _draw_pill(draw, (660, 124), f"Missed {summary.missing_checks}", red, font_small)

    left, top, right, bottom = 88, 205, width - 58, height - 92
    draw.rounded_rectangle((left, top, right, bottom), radius=12, outline="#cbd5e1", width=2, fill="#ffffff")

    if not points:
        message = "No saved checks for this period yet"
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
    latest_text = f"Latest: {position_label(latest.position)} at {latest.checked_at:%d.%m %H:%M}"
    draw.text((left, bottom + 36), latest_text, fill=ink, font=font_small)
    draw.text((right - 330, bottom + 36), "Lower is better", fill=muted, font=font_small)

    image.save(output)
    return output


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

    font_title = _font(38, bold=True)
    font_title_small = _font(34, bold=True)
    font_subtitle = _font(22)
    font_label = _font(20, bold=True)
    font_small = _font(18)
    font_tiny = _font(14)
    font_note = _font(13)

    note_box = (842, 70, width - 62, 158)
    left_margin = 54
    supplier = (target.own_supplier_name or target.label()).strip() or "магазина"
    title = f"Позиция карточки фирмы {supplier} на WB за неделю"
    title_font = font_title
    if draw.textbbox((0, 0), title, font=title_font)[2] > note_box[0] - left_margin - 28:
        title_font = font_title_small
    draw.text((left_margin, 28), title, fill=ink, font=title_font)
    draw.text((left_margin, 92), f"Запрос: {target.search_query}", fill=muted, font=font_subtitle)
    draw.text((left_margin, 123), f"Карточка: {target.label()} | {week_range.label()}", fill=muted, font=font_subtitle)

    _draw_explanation_note(draw, note_box, max_search_pages, red, red_border, ink, font_small, font_note)
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
    red: str,
    border: str,
    ink: str,
    font_title: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_body: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    draw.rounded_rectangle(box, radius=12, fill="#fff8f1", outline=border, width=2)
    center_x = (box[0] + box[2]) / 2
    draw.text((center_x, box[1] + 18), "Не найдена", fill=red, font=font_title, anchor="mm")
    draw.text(
        (center_x, box[1] + 45),
        "Карточка не найдена на первых",
        fill=ink,
        font=font_body,
        anchor="mm",
    )
    draw.text(
        (center_x, box[1] + 65),
        f"{max_search_pages} страницах WB",
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


def _draw_cross(draw: ImageDraw.ImageDraw, x: float, y: float, color: str) -> None:
    size = 9
    draw.line((x - size, y - size, x + size, y + size), fill=color, width=4)
    draw.line((x - size, y + size, x + size, y - size), fill=color, width=4)
