from __future__ import annotations

from .models import PositionAnalysis, SearchResultItem


def money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g} ₽"


def item_line(item: SearchResultItem) -> str:
    return (
        f"{item.rank}. {item.name}\n"
        f"   Магазин: {item.seller_label()}\n"
        f"   Цена: {money(item.sale_price or item.price)}\n"
        f"   {item.url}"
    )


def position_text(analysis: PositionAnalysis) -> str:
    if analysis.own_position is None:
        return f"не найдена за {analysis.pages_checked} стр."
    if analysis.own_position <= 5:
        return f"#{analysis.own_position} в топ-5"
    return f"#{analysis.own_position}"


def format_analysis(analysis: PositionAnalysis) -> str:
    lines = [
        f"Запрос: {analysis.query}",
        f"Карточка: {analysis.target.label()}",
        f"Позиция твоей карточки: {position_text(analysis)}",
    ]
    if analysis.own_item:
        lines.extend(
            [
                f"Найдена как: {analysis.own_item.name}",
                f"Магазин: {analysis.own_item.seller_label()}",
                f"Ссылка: {analysis.own_item.url}",
            ]
        )
    if analysis.warnings:
        lines.append("Предупреждения:")
        lines.extend(f"- {warning}" for warning in analysis.warnings)

    lines.append("")
    lines.append("Топ-5 выдачи:")
    if analysis.top_items:
        lines.extend(item_line(item) for item in analysis.top_items)
    else:
        lines.append("Выдача пустая или недоступна.")
    return "\n".join(lines)


def format_full_report_messages(analyses: list[PositionAnalysis]) -> list[str]:
    if not analyses:
        return []
    messages = [f"Отчет WB по позициям\nПроверено запросов: {len(analyses)}"]
    for index, analysis in enumerate(analyses, start=1):
        messages.append(f"Запрос {index}/{len(analyses)}\n\n{format_analysis(analysis)}")
    return messages


def format_short_summary(analyses: list[PositionAnalysis]) -> str:
    lines = ["Отчет WB по позициям"]
    for analysis in analyses:
        lines.append(
            f"\n{analysis.query}\n"
            f"Позиция: {position_text(analysis)}\n"
            f"Топ-5 выдачи:\n{format_short_top_items(analysis.top_items)}"
        )
    return "\n".join(lines)


def format_short_top_items(items: list[SearchResultItem]) -> str:
    if not items:
        return "-"
    lines = []
    for item in items[:5]:
        price = money(item.sale_price or item.price)
        lines.append(f"{item.rank}. {item.name} - {item.seller_label()} - {price}")
    return "\n".join(lines)
