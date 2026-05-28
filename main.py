from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wb_position_bot.analytics import (
    current_week_range,
    format_history_summary,
    load_position_history,
    render_position_chart,
)
from wb_position_bot.analyzer import analyze_target
from wb_position_bot.config import get_config
from wb_position_bot.db import (
    active_targets,
    connect,
    get_target_by_id,
    get_target_by_nm_id,
    get_target_by_sku,
    save_position_check,
    upsert_target,
)
from wb_position_bot.models import ProductTarget
from wb_position_bot.report import format_analysis
from wb_position_bot.wildberries import WildberriesClient, WildberriesError


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _client() -> WildberriesClient:
    config = get_config(require_telegram=False)
    return WildberriesClient(
        dest=config.wb_dest,
        currency=config.wb_currency,
        locale=config.wb_locale,
        timeout=config.request_timeout,
        request_delay_seconds=config.wb_request_delay_seconds,
        request_delay_jitter_seconds=config.wb_request_delay_jitter_seconds,
        retries=config.wb_request_retries,
        rate_limit_cooldown_seconds=config.wb_429_cooldown_seconds,
        proxy_url=config.wb_proxy_url,
        proxy_auth_token=config.wb_proxy_auth_token,
        proxy_insecure_ssl=config.wb_proxy_insecure_ssl,
    )


def _target_from_args(conn, args) -> ProductTarget:
    if getattr(args, "id", None):
        target = get_target_by_id(conn, args.id)
    elif getattr(args, "nm_id", None):
        target = get_target_by_nm_id(conn, args.nm_id)
    elif getattr(args, "sku", None):
        target = get_target_by_sku(conn, args.sku)
    else:
        raise SystemExit("Укажи --id, --nm-id или --sku.")
    if not target:
        raise SystemExit("Карточка не найдена в базе.")
    return target


def cmd_init_db(args) -> int:
    config = get_config(require_telegram=False)
    connect(config.database_path).close()
    print(f"База готова: {config.database_path}")
    return 0


def cmd_add(args) -> int:
    config = get_config(require_telegram=False)
    conn = connect(config.database_path)
    target = ProductTarget(
        nm_id=args.nm_id,
        sku=args.sku or (str(args.nm_id) if args.nm_id else ""),
        name=args.name or (f"WB {args.nm_id}" if args.nm_id else args.query),
        search_query=args.query,
        own_supplier_id=args.supplier_id,
        own_supplier_name=args.supplier or "",
        note=args.note or "",
        active=not args.inactive,
    )
    saved = upsert_target(conn, target)
    print(f"Сохранено: id={saved.id}, nm_id={saved.nm_id}, query={saved.search_query!r}")
    return 0


def cmd_list(args) -> int:
    config = get_config(require_telegram=False)
    conn = connect(config.database_path)
    targets = active_targets(conn, include_inactive=args.all)
    if not targets:
        print("В базе пока нет карточек.")
        return 0
    for target in targets:
        status = "active" if target.active else "inactive"
        print(
            f"{target.id}: nm_id={target.nm_id or '-'} | sku={target.sku or '-'} | "
            f"{status} | {target.search_query}"
        )
    return 0


def cmd_analyze(args) -> int:
    config = get_config(require_telegram=False)
    conn = connect(config.database_path)
    target = _target_from_args(conn, args)
    try:
        analysis = analyze_target(target, _client(), max_pages=args.pages or config.wb_max_search_pages)
    except WildberriesError as error:
        print(f"Ошибка WB: {error}", file=sys.stderr)
        return 2
    save_position_check(conn, analysis)
    print(format_analysis(analysis))
    return 0


def cmd_analyze_all(args) -> int:
    config = get_config(require_telegram=False)
    conn = connect(config.database_path)
    targets = active_targets(conn)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("Нет активных карточек для анализа.")
        return 0

    client = _client()
    exit_code = 0
    for index, target in enumerate(targets, start=1):
        print(f"\n[{index}/{len(targets)}] {target.search_query}")
        try:
            analysis = analyze_target(target, client, max_pages=args.pages or config.wb_max_search_pages)
        except WildberriesError as error:
            print(f"Ошибка WB: {error}", file=sys.stderr)
            exit_code = 2
            continue
        save_position_check(conn, analysis)
        print(format_analysis(analysis))
    return exit_code


def _chart_output(args, prefix: str, target: ProductTarget, suffix: str) -> Path:
    if getattr(args, "output", None):
        return Path(args.output)
    raw_id = str(target.nm_id or target.id or target.sku or "target")
    safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw_id)
    return Path("reports") / f"{prefix}-{safe_id}-{suffix}.png"


def cmd_week(args) -> int:
    config = get_config(require_telegram=False)
    conn = connect(config.database_path)
    target = _target_from_args(conn, args)
    week = current_week_range(config.timezone)
    points = load_position_history(conn, target, config.timezone, start=week.start, end=week.end)
    output = _chart_output(args, "week", target, week.key)
    render_position_chart(
        target,
        points,
        output,
        title=f"WB week {week.key}",
        subtitle=f"{target.search_query} | {week.label()}",
        x_start=week.start,
        x_end=week.end,
    )
    print(format_history_summary(target, points, f"Текущая неделя {week.label()}"))
    print(f"График сохранен: {output}")
    return 0


def cmd_stats(args) -> int:
    config = get_config(require_telegram=False)
    conn = connect(config.database_path)
    target = _target_from_args(conn, args)
    points = load_position_history(conn, target, config.timezone)
    output = _chart_output(args, "stats", target, "all-time")
    render_position_chart(
        target,
        points,
        output,
        title="WB all-time positions",
        subtitle=target.search_query,
    )
    print(format_history_summary(target, points, "Статистика за все время"))
    print(f"График сохранен: {output}")
    return 0


def cmd_bot(args) -> int:
    from wb_position_bot.bot import main as bot_main

    bot_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WB position tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    init_db = sub.add_parser("init-db", help="создать/обновить базу")
    init_db.set_defaults(func=cmd_init_db)

    add = sub.add_parser("add", help="добавить или обновить карточку")
    add.add_argument("--nm-id", type=int, default=None, help="артикул WB / nmId, если уже известен")
    add.add_argument("--sku", default="", help="внутренний SKU, если нужен")
    add.add_argument("--name", default="", help="название карточки для себя")
    add.add_argument("--query", required=True, help="поисковый запрос для анализа")
    add.add_argument("--supplier-id", type=int, default=None, help="ID своего продавца на WB")
    add.add_argument("--supplier", default="", help="название своего магазина на WB")
    add.add_argument("--note", default="", help="заметка")
    add.add_argument("--inactive", action="store_true", help="сохранить выключенной")
    add.set_defaults(func=cmd_add)

    list_cmd = sub.add_parser("list", help="показать карточки")
    list_cmd.add_argument("--all", action="store_true", help="включая выключенные")
    list_cmd.set_defaults(func=cmd_list)

    analyze = sub.add_parser("analyze", help="проверить одну карточку")
    analyze.add_argument("--id", type=int)
    analyze.add_argument("--nm-id", type=int)
    analyze.add_argument("--sku")
    analyze.add_argument("--pages", type=int, default=0, help="сколько страниц выдачи смотреть")
    analyze.set_defaults(func=cmd_analyze)

    analyze_all = sub.add_parser("analyze-all", help="проверить все активные карточки")
    analyze_all.add_argument("--limit", type=int, default=0)
    analyze_all.add_argument("--pages", type=int, default=0, help="сколько страниц выдачи смотреть")
    analyze_all.set_defaults(func=cmd_analyze_all)

    week = sub.add_parser("week", help="построить график текущей недели")
    week.add_argument("--id", type=int)
    week.add_argument("--nm-id", type=int)
    week.add_argument("--sku")
    week.add_argument("--output", default="", help="куда сохранить PNG")
    week.set_defaults(func=cmd_week)

    stats = sub.add_parser("stats", help="построить график и статистику за все время")
    stats.add_argument("--id", type=int)
    stats.add_argument("--nm-id", type=int)
    stats.add_argument("--sku")
    stats.add_argument("--output", default="", help="куда сохранить PNG")
    stats.set_defaults(func=cmd_stats)

    bot = sub.add_parser("bot", help="запустить Telegram-бота")
    bot.set_defaults(func=cmd_bot)

    return parser


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
