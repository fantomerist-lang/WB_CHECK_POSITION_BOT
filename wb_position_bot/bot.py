from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from datetime import datetime, time

from telegram import Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes

from .analytics import (
    PositionSeries,
    current_week_range,
    format_all_targets_summary,
    format_history_summary,
    load_position_history,
    render_position_chart,
    render_marketplace_overview_chart,
    render_week_position_chart,
)
from .analyzer import analyze_target
from .config import Config, get_config
from .db import (
    active_targets,
    connect,
    get_target_by_external_id,
    get_target_by_id,
    get_setting,
    get_target_by_nm_id,
    save_position_check,
    set_target_active,
    set_setting,
    upsert_target,
)
from .models import ProductTarget
from .report import format_analysis, format_full_report_messages
from .target_parser import parse_add_args
from .wildberries import WildberriesClient, WildberriesError
from .yandex_market import YandexMarketClient, YandexMarketError


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def db_path(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.application.bot_data["config"].database_path


def wb_client(context: ContextTypes.DEFAULT_TYPE) -> WildberriesClient:
    config: Config = context.application.bot_data["config"]
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


def ym_client(context: ContextTypes.DEFAULT_TYPE) -> YandexMarketClient:
    config: Config = context.application.bot_data["config"]
    return YandexMarketClient(
        region_id=config.ym_region_id,
        timeout=config.request_timeout,
        request_delay_seconds=config.ym_request_delay_seconds,
        request_delay_jitter_seconds=config.ym_request_delay_jitter_seconds,
        retries=config.ym_request_retries,
        proxy_url=config.ym_proxy_url,
        proxy_auth_token=config.ym_proxy_auth_token,
        proxy_insecure_ssl=config.ym_proxy_insecure_ssl,
        enrich_sellers=config.ym_enrich_sellers,
    )


def client_for_target(context: ContextTypes.DEFAULT_TYPE, target: ProductTarget):
    return ym_client(context) if target.marketplace == "ym" else wb_client(context)


def max_pages_for_target(config: Config, target: ProductTarget) -> int:
    return config.ym_max_search_pages if target.marketplace == "ym" else config.wb_max_search_pages


def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    config: Config = context.application.bot_data["config"]
    if config.admin_chat_id:
        return chat.id == config.admin_chat_id
    conn = connect(config.database_path)
    saved = get_setting(conn, "admin_chat_id")
    return bool(saved and int(saved) == chat.id)


async def ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_admin(update, context):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Нет доступа. Этот бот привязан к владельцу.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.effective_message:
        return
    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)
    saved = get_setting(conn, "admin_chat_id")
    args = context.args or []

    if saved or config.admin_chat_id:
        if not is_admin(update, context):
            await update.effective_message.reply_text("Бот уже привязан к владельцу.")
            return
    elif config.setup_key and config.setup_key != "change-me" and (not args or args[0] != config.setup_key):
        await update.effective_message.reply_text("Для первого запуска напиши /start SETUP_KEY.")
        return
    else:
        set_setting(conn, "admin_chat_id", str(chat.id))

    await update.effective_message.reply_text(
        "Готов. Команды:\n"
        "/status - состояние базы\n"
        "/add 123456789 | запрос | Магазин - добавить Wildberries\n"
        "/addym 103705469335 | запрос | Магазин - добавить Яндекс Маркет\n"
        "/list - список карточек\n"
        "/delete 1 - убрать запрос из отслеживания, сохранив историю\n"
        "/check 1 - проверить запись по id из /list\n"
        "/checkall - проверить все активные карточки\n"
        "/week 1 - график текущей недели по id из /list\n"
        "/weekwb - все запросы WB за текущую неделю\n"
        "/weekym - все запросы Яндекс Маркета за текущую неделю\n"
        "/stats 1 - статистика за все время по id из /list\n"
        "/statswb - все запросы WB за все время\n"
        "/statsym - все запросы Яндекс Маркета за все время\n"
        "/stats - краткая статистика по всем карточкам"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)
    targets = active_targets(conn, include_inactive=True)
    active_count = len([target for target in targets if target.active])
    wb_count = len([target for target in targets if target.marketplace == "wb"])
    ym_count = len([target for target in targets if target.marketplace == "ym"])
    await update.effective_message.reply_text(
        f"Карточек в базе: {len(targets)}\n"
        f"Wildberries: {wb_count}\n"
        f"Яндекс Маркет: {ym_count}\n"
        f"Активных: {active_count}\n"
        f"Автоотчеты: {', '.join(config.report_times)} каждые {config.report_interval_days} дн.\n"
        f"Страниц: WB {config.wb_max_search_pages}, Яндекс {config.ym_max_search_pages}"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    try:
        target = parse_add_args(update.effective_message.text or "", marketplace="wb")
    except (TypeError, ValueError) as error:
        await update.effective_message.reply_text(str(error))
        return
    conn = connect(db_path(context))
    saved = upsert_target(conn, target)
    await update.effective_message.reply_text(
        f"Сохранено: id={saved.id}, площадка={saved.marketplace_label()}, карточка={saved.product_id()}"
    )


async def add_yandex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    try:
        target = parse_add_args(update.effective_message.text or "", marketplace="ym")
    except (TypeError, ValueError) as error:
        await update.effective_message.reply_text(str(error))
        return
    conn = connect(db_path(context))
    saved = upsert_target(conn, target)
    await update.effective_message.reply_text(
        f"Сохранено: id={saved.id}, площадка={saved.marketplace_label()}, карточка={saved.product_id()}"
    )


async def list_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    conn = connect(db_path(context))
    targets = active_targets(conn)
    if not targets:
        await update.effective_message.reply_text("Активных карточек пока нет.")
        return
    lines = [
        f"{target.id}: [{target.marketplace_label()}] {target.product_id() or '-'} | {target.search_query}"
        for target in targets[:50]
    ]
    if len(targets) > 50:
        lines.append(f"...и еще {len(targets) - 50}")
    await update.effective_message.reply_text("\n".join(lines))


def target_by_number(conn, value: int) -> ProductTarget | None:
    return (
        get_target_by_id(conn, value)
        or get_target_by_nm_id(conn, value)
        or get_target_by_external_id(conn, "ym", str(value))
    )


async def set_active_command(update: Update, context: ContextTypes.DEFAULT_TYPE, active: bool) -> None:
    if not await ensure_admin(update, context):
        return
    args = context.args or []
    command = "enable" if active else "disable"
    if not args:
        await update.effective_message.reply_text(f"Напиши /{command} nm_id или id из /list.")
        return
    try:
        value = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("ID должен быть числом.")
        return

    conn = connect(db_path(context))
    target = target_by_number(conn, value)
    if not target or not target.id:
        await update.effective_message.reply_text("Карточка не найдена в базе.")
        return

    saved = set_target_active(conn, target.id, active)
    status = "включена" if active else "выключена"
    await update.effective_message.reply_text(f"Запись {saved.id if saved else target.id} {status}: {target.search_query}")


async def disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_active_command(update, context, active=False)


async def enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_active_command(update, context, active=True)


async def delete_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Напиши /delete ID из /list.")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("ID должен быть числом.")
        return

    conn = connect(db_path(context))
    target = get_target_by_id(conn, target_id)
    if not target:
        await update.effective_message.reply_text("Запрос с таким id не найден в базе.")
        return
    if not target.active:
        await update.effective_message.reply_text(
            f"Запрос {target.id} уже удалён из отслеживания. Его история сохранена."
        )
        return

    set_target_active(conn, target_id, False)
    await update.effective_message.reply_text(
        f"Запрос удалён из ежедневного отслеживания: {target.search_query}\n"
        "Собранная история сохранена и останется на недельных и общих графиках до даты удаления."
    )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Напиши /check nm_id или id из /list.")
        return
    try:
        value = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("ID должен быть числом.")
        return
    conn = connect(db_path(context))
    target = target_by_number(conn, value)
    if not target:
        await update.effective_message.reply_text("Карточка не найдена в базе.")
        return
    await update.effective_message.reply_text(f"Проверяю выдачу {target.marketplace_label()}...")
    config: Config = context.application.bot_data["config"]
    try:
        analysis = await asyncio.to_thread(
            analyze_target,
            target,
            client_for_target(context, target),
            max_pages_for_target(config, target),
        )
    except (WildberriesError, YandexMarketError) as error:
        await update.effective_message.reply_text(f"Ошибка {target.marketplace_label()}: {error}")
        return
    save_position_check(conn, analysis, check_source="manual")
    await update.effective_message.reply_text(format_analysis(analysis), disable_web_page_preview=True)


async def checkall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await run_checks_for_chat(context, update.effective_chat.id)


def chart_path(context: ContextTypes.DEFAULT_TYPE, prefix: str, target: ProductTarget, suffix: str) -> Path:
    database_path = Path(db_path(context))
    reports_dir = database_path.parent / "reports"
    raw_id = f"{target.marketplace}-{target.product_id() or target.id or target.sku or 'target'}"
    safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw_id)
    return reports_dir / f"{prefix}-{safe_id}-{suffix}.png"


def marketplace_chart_path(
    context: ContextTypes.DEFAULT_TYPE,
    prefix: str,
    marketplace: str,
    suffix: str,
) -> Path:
    database_path = Path(db_path(context))
    reports_dir = database_path.parent / "reports"
    return reports_dir / f"{prefix}-{marketplace}-{suffix}.png"


def marketplace_series(
    conn,
    targets: list[ProductTarget],
    config: Config,
    marketplace: str,
    start=None,
    end=None,
) -> list[PositionSeries]:
    result: list[PositionSeries] = []
    for target in targets:
        if target.marketplace != marketplace:
            continue
        points = load_position_history(
            conn,
            target,
            config.timezone,
            start=start,
            end=end,
            check_source="auto",
        )
        result.append(PositionSeries(target=target, points=points))
    return result


async def send_marketplace_overview(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    marketplace: str,
    all_time: bool = False,
    notify_if_empty: bool = True,
) -> bool:
    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)
    targets = active_targets(conn, include_inactive=True)
    marketplace_targets = [target for target in targets if target.marketplace == marketplace]
    marketplace_label = "Яндекс Маркет" if marketplace == "ym" else "Wildberries"
    if not marketplace_targets:
        if notify_if_empty:
            await safe_send_message(context, chat_id, f"Нет активных запросов для {marketplace_label}.")
        return False

    if all_time:
        series = marketplace_series(conn, marketplace_targets, config, marketplace)
        output = marketplace_chart_path(context, "stats", marketplace, "all-time")
        period_title = "Статистика за все время"
        x_start = None
        x_end = None
        suffix = "за все время"
    else:
        week_range = current_week_range(config.timezone)
        series = marketplace_series(
            conn,
            marketplace_targets,
            config,
            marketplace,
            start=week_range.start,
            end=week_range.end,
        )
        output = marketplace_chart_path(context, "week", marketplace, week_range.key)
        period_title = f"Неделя {week_range.label()}"
        x_start = week_range.start
        x_end = week_range.end
        suffix = "за текущую неделю"

    series = [item for item in series if item.target.active or item.points]
    if not series:
        if notify_if_empty:
            await safe_send_message(context, chat_id, f"Пока нет сохранённых проверок для {marketplace_label}.")
        return False

    try:
        render_marketplace_overview_chart(
            series,
            output,
            marketplace=marketplace,
            period_title=period_title,
            x_start=x_start,
            x_end=x_end,
            weekly=not all_time,
            max_search_pages=(
                config.ym_max_search_pages if marketplace == "ym" else config.wb_max_search_pages
            ),
        )
    except RuntimeError as error:
        await safe_send_message(context, chat_id, f"Не удалось построить график {marketplace_label}: {error}")
        return False

    await safe_send_photo(
        context,
        chat_id,
        output,
        caption=f"{marketplace_label}: все поисковые запросы {suffix}",
    )
    return True


async def target_from_first_arg(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> ProductTarget | None:
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(f"Напиши /{command} nm_id или id из /list.")
        return None
    try:
        value = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("ID должен быть числом.")
        return None
    conn = connect(db_path(context))
    target = target_by_number(conn, value)
    if not target:
        await update.effective_message.reply_text("Карточка не найдена в базе.")
        return None
    return target


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    target = await target_from_first_arg(update, context, "week")
    if not target:
        return

    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)
    week_range = current_week_range(config.timezone)
    points = load_position_history(
        conn,
        target,
        config.timezone,
        start=week_range.start,
        end=week_range.end,
        check_source="auto",
    )
    output = chart_path(context, "week", target, week_range.key)
    try:
        render_week_position_chart(
            target,
            points,
            output,
            week_range,
            max_search_pages=max_pages_for_target(config, target),
        )
    except RuntimeError as error:
        await update.effective_message.reply_text(f"Не удалось построить график: {error}")
        return
    await safe_send_message(
        context,
        update.effective_chat.id,
        format_history_summary(target, points, f"Текущая неделя {week_range.label()} (только автоотчеты)"),
    )
    await safe_send_photo(context, update.effective_chat.id, output, caption="График текущей недели")


async def week_wb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await send_marketplace_overview(context, update.effective_chat.id, "wb")


async def week_yandex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await send_marketplace_overview(context, update.effective_chat.id, "ym")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)

    if not context.args:
        targets = active_targets(conn, include_inactive=True)
        await safe_send_message(context, update.effective_chat.id, format_all_targets_summary(conn, targets, config.timezone))
        return

    target = await target_from_first_arg(update, context, "stats")
    if not target:
        return

    points = load_position_history(conn, target, config.timezone)
    output = chart_path(context, "stats", target, "all-time")
    try:
        render_position_chart(
            target,
            points,
            output,
            title=f"{target.marketplace_label()}: позиции за все время",
            subtitle=f"{target.search_query} | {target.label()}",
        )
    except RuntimeError as error:
        await update.effective_message.reply_text(f"Не удалось построить график: {error}")
        return
    await safe_send_message(
        context,
        update.effective_chat.id,
        format_history_summary(target, points, "Статистика за все время"),
    )
    await safe_send_photo(context, update.effective_chat.id, output, caption="График за все время")


async def stats_wb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await send_marketplace_overview(context, update.effective_chat.id, "wb", all_time=True)


async def stats_yandex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await send_marketplace_overview(context, update.effective_chat.id, "ym", all_time=True)


def should_run_auto_report(conn, config: Config) -> bool:
    if config.report_interval_days <= 1:
        return True
    today = datetime.now(config.timezone).date().isoformat()
    last = get_setting(conn, "last_auto_report_date")
    if not last:
        set_setting(conn, "last_auto_report_date", today)
        return True
    try:
        last_date = datetime.fromisoformat(last).date()
    except ValueError:
        set_setting(conn, "last_auto_report_date", today)
        return True
    if (datetime.now(config.timezone).date() - last_date).days >= config.report_interval_days:
        set_setting(conn, "last_auto_report_date", today)
        return True
    return False


async def run_checks_for_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, auto: bool = False) -> None:
    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)
    if auto and not should_run_auto_report(conn, config):
        return
    targets = active_targets(conn)
    if not targets:
        await context.bot.send_message(chat_id=chat_id, text="Нет активных карточек для проверки.")
        return

    clients = {
        "wb": wb_client(context),
        "ym": ym_client(context),
    }
    analyses = []
    for target in targets:
        try:
            analysis = await asyncio.to_thread(
                analyze_target,
                target,
                clients[target.marketplace],
                max_pages_for_target(config, target),
            )
        except (WildberriesError, YandexMarketError) as error:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Ошибка {target.marketplace_label()} для {target.search_query}: {error}",
            )
            continue
        save_position_check(conn, analysis, check_source="auto" if auto else "manual")
        analyses.append(analysis)

    if analyses:
        for message in format_full_report_messages(analyses):
            await safe_send_message(context, chat_id, message, disable_web_page_preview=True)

    if auto:
        await send_marketplace_overview(context, chat_id, "wb", notify_if_empty=False)
        await send_marketplace_overview(context, chat_id, "ym", notify_if_empty=False)


async def safe_send_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    disable_web_page_preview: bool = True,
    retries: int = 3,
) -> None:
    for attempt in range(1, retries + 1):
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text[:3900],
                disable_web_page_preview=disable_web_page_preview,
            )
            return
        except RetryAfter as error:
            await asyncio.sleep(float(error.retry_after or 3) + 0.5)
        except (TimedOut, NetworkError):
            if attempt >= retries:
                raise
            await asyncio.sleep(1.5 * attempt)
        except TelegramError:
            raise


async def safe_send_photo(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    path: Path,
    caption: str = "",
    retries: int = 3,
) -> None:
    for attempt in range(1, retries + 1):
        try:
            with path.open("rb") as photo:
                await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption[:1000])
            return
        except RetryAfter as error:
            await asyncio.sleep(float(error.retry_after or 3) + 0.5)
        except (TimedOut, NetworkError):
            if attempt >= retries:
                raise
            await asyncio.sleep(1.5 * attempt)
        except TelegramError:
            raise


async def scheduled_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)
    chat_id = config.admin_chat_id or get_setting(conn, "admin_chat_id")
    if not chat_id:
        log.info("No admin chat id yet; scheduled report skipped")
        return
    await run_checks_for_chat(context, int(chat_id), auto=True)


def schedule_reports(app: Application, config: Config) -> None:
    if not app.job_queue:
        log.warning("Job queue is unavailable; scheduled reports disabled")
        return
    for value in config.report_times:
        hour, minute = [int(part) for part in value.split(":", 1)]
        app.job_queue.run_daily(scheduled_report, time=time(hour, minute, tzinfo=config.timezone))
        log.info("Scheduled report at %s %s", value, config.timezone)


def main() -> None:
    config = get_config(require_telegram=True)
    app = Application.builder().token(config.telegram_token).build()
    app.bot_data["config"] = config
    connect(config.database_path).close()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("addym", add_yandex))
    app.add_handler(CommandHandler("list", list_targets))
    app.add_handler(CommandHandler("disable", disable))
    app.add_handler(CommandHandler("enable", enable))
    app.add_handler(CommandHandler("delete", delete_query))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("checkall", checkall))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("weekwb", week_wb))
    app.add_handler(CommandHandler("weekym", week_yandex))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("statswb", stats_wb))
    app.add_handler(CommandHandler("statsym", stats_yandex))

    schedule_reports(app, config)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
