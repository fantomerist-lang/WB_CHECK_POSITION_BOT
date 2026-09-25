from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from dataclasses import replace
from pathlib import Path
from datetime import datetime, time, timedelta, timezone

from telegram import Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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
    active_authorized_users,
    active_targets,
    authorize_user,
    claim_unowned_targets,
    connect,
    delete_setting,
    disable_targets_for_owner,
    get_authorized_user,
    get_target_by_id,
    get_setting,
    revoke_user,
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
        reef_api_key=config.reef_api_key,
        reef_api_url=config.reef_api_url,
        reef_country=config.reef_country,
    )


def ym_client(context: ContextTypes.DEFAULT_TYPE) -> YandexMarketClient:
    config: Config = context.application.bot_data["config"]
    return YandexMarketClient(
        region_id=config.ym_region_id,
        timeout=config.ym_request_timeout,
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


def check_lock(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
    lock = context.application.bot_data.get("marketplace_check_lock")
    if lock is None:
        lock = asyncio.Lock()
        context.application.bot_data["marketplace_check_lock"] = lock
    return lock


def analyze_target_bounded(
    target: ProductTarget,
    client,
    max_pages: int,
    timeout_seconds: float,
):
    start_operation = getattr(client, "start_operation", None)
    finish_operation = getattr(client, "finish_operation", None)
    if start_operation:
        start_operation(timeout_seconds)
    try:
        return analyze_target(target, client, max_pages)
    finally:
        if finish_operation:
            finish_operation()


def configured_admin_chat_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    config: Config = context.application.bot_data["config"]
    if config.admin_chat_id:
        return config.admin_chat_id
    conn = connect(config.database_path)
    saved = get_setting(conn, "admin_chat_id")
    return int(saved) if saved else None


def is_admin_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    return configured_admin_chat_id(context) == int(chat_id)


def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    return bool(chat and is_admin_chat(context, chat.id))


def is_authorized_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    if is_admin_chat(context, chat_id):
        return True
    conn = connect(db_path(context))
    return get_authorized_user(conn, chat_id) is not None


async def ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_admin(update, context):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Нет доступа. Этот бот привязан к владельцу.")
    return False


async def ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat and is_authorized_chat(context, chat.id):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Нет доступа. Попроси владельца создать приглашение командой /invite.")
    return False


HELP_TEXT = """КАК РАБОТАЕТ БОТ

Каждая запись связывает площадку, карточку и поисковый запрос. Ежедневно бот вводит запрос в поиск, находит позицию карточки, сохраняет результат и присылает отчет. Ручные проверки нужны для теста; недельные общие графики строятся по автоматическим проверкам.

ДОБАВЛЕНИЕ ЗАПРОСА WILDBERRIES
/add АРТИКУЛ_КАРТОЧКИ | ПОИСКОВЫЙ_ЗАПРОС | НАЗВАНИЕ_МАГАЗИНА

• АРТИКУЛ_КАРТОЧКИ — номер карточки вашей фирмы на Wildberries.
• ПОИСКОВЫЙ_ЗАПРОС — фраза, которую покупатель вводит в поиск WB.
• НАЗВАНИЕ_МАГАЗИНА — ваш магазин или продавец на WB.

Бот введет указанный запрос в поиск Wildberries и покажет, на каком месте находится именно эта карточка.

Пример:
/add 399568521 | 1с бухгалтерия базовая | Кодерлайн

ДОБАВЛЕНИЕ ЗАПРОСА ЯНДЕКС МАРКЕТА
/addym ID_КАРТОЧКИ_ИЛИ_ССЫЛКА | ПОИСКОВЫЙ_ЗАПРОС | НАЗВАНИЕ_МАГАЗИНА

• ID_КАРТОЧКИ_ИЛИ_ССЫЛКА — номер или полная ссылка на карточку вашей фирмы в Яндекс Маркете.
• ПОИСКОВЫЙ_ЗАПРОС — фраза, которую покупатель вводит в поиск Яндекс Маркета.
• НАЗВАНИЕ_МАГАЗИНА — ваш магазин или продавец в Яндекс Маркете.

Бот введет запрос в поиск Яндекс Маркета и покажет позицию указанной карточки.

Пример:
/addym 4717385177 | 1с бухгалтерия базовая | Кодерлайн

Чтобы отслеживать одну карточку по нескольким поисковым фразам, добавь каждую фразу отдельной командой.

УПРАВЛЕНИЕ
/list — активные записи и их ID
/status — состояние базы и расписания
/disable ID — приостановить ежедневную проверку
/enable ID — возобновить проверку
/delete ID — убрать из отслеживания, сохранив историю

ПРОВЕРКИ
/check ID — проверить одну запись сейчас
/checkall — проверить все доступные активные записи

ГРАФИКИ
/week ID — неделя по одной записи
/weekwb — неделя по всем доступным запросам WB
/weekym — неделя по всем доступным запросам Яндекс Маркета
/stats — краткая статистика
/stats ID — одна запись за всё время
/statswb — все запросы WB за всё время
/statsym — все запросы Яндекс Маркета за всё время

ДОСТУП
/invite — создать приглашение для второго пользователя (только владелец)
/users — показать пользователей (только владелец)
/removeuser CHAT_ID — закрыть доступ (только владелец)

У каждого приглашенного пользователя отдельные запросы. Он не видит записи владельца; владелец видит все записи и получает копии его команд.

/start — запустить бота
/help — снова показать эту инструкцию

ID записи берется из /list."""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
        return
    if update.effective_message:
        await update.effective_message.reply_text(HELP_TEXT)


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
            if get_authorized_user(conn, chat.id):
                await update.effective_message.reply_text(f"Готов. У тебя отдельный набор запросов.\n\n{HELP_TEXT}")
                return
            invite_code = get_setting(conn, "member_invite_code") or ""
            invite_expires = get_setting(conn, "member_invite_expires") or ""
            supplied_code = args[0] if args else ""
            try:
                expires_at = datetime.fromisoformat(invite_expires)
            except ValueError:
                expires_at = datetime.min.replace(tzinfo=timezone.utc)
            invite_valid = (
                bool(invite_code)
                and bool(supplied_code)
                and hmac.compare_digest(invite_code, supplied_code)
                and expires_at > datetime.now(timezone.utc)
            )
            if not invite_valid:
                await update.effective_message.reply_text(
                    "Бот уже привязан к владельцу. Для доступа попроси у него одноразовую команду приглашения."
                )
                return
            if active_authorized_users(conn):
                await update.effective_message.reply_text("Место второго пользователя уже занято.")
                return
            user = update.effective_user
            authorize_user(
                conn,
                chat.id,
                username=user.username if user else "",
                display_name=user.full_name if user else "",
            )
            delete_setting(conn, "member_invite_code")
            delete_setting(conn, "member_invite_expires")
            admin_id = configured_admin_chat_id(context)
            if admin_id:
                label = user.full_name if user else str(chat.id)
                await safe_send_message(
                    context,
                    admin_id,
                    f"Подключен второй пользователь: {label} (chat_id {chat.id}).",
                )
            await update.effective_message.reply_text(f"Доступ открыт. У тебя отдельный набор запросов.\n\n{HELP_TEXT}")
            return
    elif config.setup_key and config.setup_key != "change-me" and (not args or args[0] != config.setup_key):
        await update.effective_message.reply_text("Для первого запуска напиши /start SETUP_KEY.")
        return
    else:
        set_setting(conn, "admin_chat_id", str(chat.id))

    claim_unowned_targets(conn, chat.id)

    await update.effective_message.reply_text(f"Готов.\n\n{HELP_TEXT}")


async def invite_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    conn = connect(db_path(context))
    users = active_authorized_users(conn)
    if users:
        user = users[0]
        label = str(user["display_name"] or user["username"] or user["chat_id"])
        await update.effective_message.reply_text(
            f"Второй пользователь уже подключен: {label} (chat_id {user['chat_id']}).\n"
            "Сначала закрой ему доступ командой /removeuser CHAT_ID."
        )
        return

    code = secrets.token_urlsafe(6)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    set_setting(conn, "member_invite_code", code)
    set_setting(conn, "member_invite_expires", expires_at.isoformat())
    await update.effective_message.reply_text(
        "Перешли второму пользователю эту команду:\n\n"
        f"/start {code}\n\n"
        "Код одноразовый и действует 24 часа."
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    conn = connect(db_path(context))
    admin_id = configured_admin_chat_id(context)
    lines = [f"Владелец: chat_id {admin_id}"]
    users = active_authorized_users(conn)
    if not users:
        lines.append("Второй пользователь: не подключен")
    for user in users:
        label = str(user["display_name"] or user["username"] or "без имени")
        username = f"@{user['username']}" if user["username"] else "без username"
        lines.append(f"Пользователь: {label}, {username}, chat_id {user['chat_id']}")
    await update.effective_message.reply_text("\n".join(lines))


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Формат: /removeuser CHAT_ID. Узнать ID можно через /users.")
        return
    try:
        chat_id = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("CHAT_ID должен быть числом.")
        return
    conn = connect(db_path(context))
    if not revoke_user(conn, chat_id):
        await update.effective_message.reply_text("Активный пользователь с таким CHAT_ID не найден.")
        return
    disable_targets_for_owner(conn, chat_id)
    await update.effective_message.reply_text(
        f"Доступ пользователя {chat_id} закрыт. Его запросы остановлены, история сохранена."
    )
    try:
        await safe_send_message(context, chat_id, "Владелец закрыл доступ к боту.")
    except TelegramError:
        pass


async def audit_member_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message or is_admin_chat(context, chat.id):
        return
    conn = connect(db_path(context))
    if not get_authorized_user(conn, chat.id):
        return
    user = update.effective_user
    authorize_user(
        conn,
        chat.id,
        username=user.username if user else "",
        display_name=user.full_name if user else "",
    )
    admin_id = configured_admin_chat_id(context)
    if not admin_id:
        return
    label = user.full_name if user else str(chat.id)
    username = f" @{user.username}" if user and user.username else ""
    await safe_send_message(
        context,
        admin_id,
        f"Команда пользователя {label}{username} (chat_id {chat.id}):\n{message.text or ''}",
    )


def visible_targets_for_chat(
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    chat_id: int,
    include_inactive: bool = False,
) -> list[ProductTarget]:
    owner_chat_id = None if is_admin_chat(context, chat_id) else chat_id
    return active_targets(conn, include_inactive=include_inactive, owner_chat_id=owner_chat_id)


def can_access_target(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target: ProductTarget) -> bool:
    return is_admin_chat(context, chat_id) or target.owner_chat_id == int(chat_id)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
        return
    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)
    chat_id = update.effective_chat.id
    targets = visible_targets_for_chat(context, conn, chat_id, include_inactive=True)
    active_count = len([target for target in targets if target.active])
    wb_count = len([target for target in targets if target.marketplace == "wb"])
    ym_count = len([target for target in targets if target.marketplace == "ym"])
    users_line = ""
    if is_admin_chat(context, chat_id):
        users_line = f"\nДопущенных пользователей: {1 + len(active_authorized_users(conn))}"
    await update.effective_message.reply_text(
        f"Карточек в базе: {len(targets)}\n"
        f"Wildberries: {wb_count}\n"
        f"Яндекс Маркет: {ym_count}\n"
        f"Активных: {active_count}\n"
        f"Автоотчеты: {', '.join(config.report_times)} каждые {config.report_interval_days} дн.\n"
        f"Страниц: WB {config.wb_max_search_pages}, Яндекс {config.ym_max_search_pages}"
        f"{users_line}"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
        return
    try:
        target = parse_add_args(update.effective_message.text or "", marketplace="wb")
    except (TypeError, ValueError) as error:
        await update.effective_message.reply_text(str(error))
        return
    target = replace(target, owner_chat_id=update.effective_chat.id)
    conn = connect(db_path(context))
    saved = upsert_target(conn, target)
    await update.effective_message.reply_text(
        f"Сохранено: id={saved.id}, площадка={saved.marketplace_label()}, карточка={saved.product_id()}"
    )


async def add_yandex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
        return
    try:
        target = parse_add_args(update.effective_message.text or "", marketplace="ym")
    except (TypeError, ValueError) as error:
        await update.effective_message.reply_text(str(error))
        return
    target = replace(target, owner_chat_id=update.effective_chat.id)
    conn = connect(db_path(context))
    saved = upsert_target(conn, target)
    await update.effective_message.reply_text(
        f"Сохранено: id={saved.id}, площадка={saved.marketplace_label()}, карточка={saved.product_id()}"
    )


async def list_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
        return
    conn = connect(db_path(context))
    chat_id = update.effective_chat.id
    targets = visible_targets_for_chat(context, conn, chat_id)
    if not targets:
        await update.effective_message.reply_text("Активных карточек пока нет.")
        return
    admin_id = configured_admin_chat_id(context)
    lines = []
    for target in targets[:50]:
        owner = ""
        if is_admin_chat(context, chat_id) and target.owner_chat_id not in {0, admin_id}:
            owner = f" | пользователь {target.owner_chat_id}"
        lines.append(
            f"{target.id}: [{target.marketplace_label()}] {target.product_id() or '-'} | "
            f"{target.search_query}{owner}"
        )
    if len(targets) > 50:
        lines.append(f"...и еще {len(targets) - 50}")
    await update.effective_message.reply_text("\n".join(lines))


def target_by_number(
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    chat_id: int,
    value: int,
) -> ProductTarget | None:
    targets = visible_targets_for_chat(context, conn, chat_id, include_inactive=True)
    for target in targets:
        if target.id == value:
            return target
    for target in targets:
        if target.nm_id == value or target.external_id == str(value):
            return target
    return None


async def set_active_command(update: Update, context: ContextTypes.DEFAULT_TYPE, active: bool) -> None:
    if not await ensure_access(update, context):
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
    target = target_by_number(context, conn, update.effective_chat.id, value)
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
    if not await ensure_access(update, context):
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
    if not target or not can_access_target(context, update.effective_chat.id, target):
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
    if not await ensure_access(update, context):
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
    target = target_by_number(context, conn, update.effective_chat.id, value)
    if not target:
        await update.effective_message.reply_text("Карточка не найдена в базе.")
        return
    lock = check_lock(context)
    if lock.locked():
        await update.effective_message.reply_text(
            "Другая проверка уже выполняется. Дождись её результата и повтори команду."
        )
        return
    config: Config = context.application.bot_data["config"]
    async with lock:
        await update.effective_message.reply_text(f"Проверяю выдачу {target.marketplace_label()}...")
        try:
            analysis = await asyncio.to_thread(
                analyze_target_bounded,
                target,
                client_for_target(context, target),
                max_pages_for_target(config, target),
                config.ym_check_timeout if target.marketplace == "ym" else 0,
            )
        except (WildberriesError, YandexMarketError) as error:
            await update.effective_message.reply_text(f"Ошибка {target.marketplace_label()}: {error}")
            return
        except Exception:
            log.exception("Unexpected marketplace check error for target id=%s", target.id)
            await update.effective_message.reply_text(
                f"Ошибка {target.marketplace_label()}: проверка аварийно остановлена. Попробуй ещё раз позже."
            )
            return
        save_position_check(conn, analysis, check_source="manual")
        await update.effective_message.reply_text(format_analysis(analysis), disable_web_page_preview=True)


async def checkall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
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
    targets = visible_targets_for_chat(context, conn, chat_id, include_inactive=True)
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
    target = target_by_number(context, conn, update.effective_chat.id, value)
    if not target:
        await update.effective_message.reply_text("Карточка не найдена в базе.")
        return None
    return target


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
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
    if not await ensure_access(update, context):
        return
    await send_marketplace_overview(context, update.effective_chat.id, "wb")


async def week_yandex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
        return
    await send_marketplace_overview(context, update.effective_chat.id, "ym")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
        return
    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)

    if not context.args:
        targets = visible_targets_for_chat(
            context,
            conn,
            update.effective_chat.id,
            include_inactive=True,
        )
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
    if not await ensure_access(update, context):
        return
    await send_marketplace_overview(context, update.effective_chat.id, "wb", all_time=True)


async def stats_yandex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
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
    lock = check_lock(context)
    if lock.locked():
        if not auto:
            await safe_send_message(
                context,
                chat_id,
                "Другая проверка уже выполняется. Дождись её результата и повтори команду.",
            )
        else:
            log.warning("Scheduled report skipped because another marketplace check is running")
        return

    async with lock:
        await run_checks_for_chat_unlocked(context, chat_id, auto=auto)


async def run_checks_for_chat_unlocked(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    auto: bool = False,
) -> None:
    config: Config = context.application.bot_data["config"]
    conn = connect(config.database_path)
    targets = visible_targets_for_chat(context, conn, chat_id)
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
                analyze_target_bounded,
                target,
                clients[target.marketplace],
                max_pages_for_target(config, target),
                config.ym_check_timeout if target.marketplace == "ym" else 0,
            )
        except (WildberriesError, YandexMarketError) as error:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Ошибка {target.marketplace_label()} для {target.search_query}: {error}",
            )
            continue
        except Exception:
            log.exception("Unexpected marketplace check error for target id=%s", target.id)
            await safe_send_message(
                context,
                chat_id,
                f"Ошибка {target.marketplace_label()} для {target.search_query}: проверка аварийно остановлена.",
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
    admin_id = configured_admin_chat_id(context)
    if not admin_id:
        log.info("No admin chat id yet; scheduled report skipped")
        return
    if not should_run_auto_report(conn, config):
        return

    lock = check_lock(context)
    if lock.locked():
        log.warning("Scheduled report skipped because another marketplace check is running")
        return

    async with lock:
        targets = active_targets(conn)
        if not targets:
            await safe_send_message(context, admin_id, "Нет активных карточек для автоматической проверки.")
            return

        clients = {"wb": wb_client(context), "ym": ym_client(context)}
        analyses_by_owner: dict[int, list] = {}
        for target in targets:
            owner_id = target.owner_chat_id or admin_id
            try:
                analysis = await asyncio.to_thread(
                    analyze_target_bounded,
                    target,
                    clients[target.marketplace],
                    max_pages_for_target(config, target),
                    config.ym_check_timeout if target.marketplace == "ym" else 0,
                )
            except (WildberriesError, YandexMarketError) as error:
                text = f"Ошибка {target.marketplace_label()} для {target.search_query}: {error}"
                await safe_send_message(context, admin_id, text)
                if owner_id != admin_id:
                    await safe_send_message(context, owner_id, text)
                continue
            except Exception:
                log.exception("Unexpected scheduled check error for target id=%s", target.id)
                text = (
                    f"Ошибка {target.marketplace_label()} для {target.search_query}: "
                    "проверка аварийно остановлена."
                )
                await safe_send_message(context, admin_id, text)
                if owner_id != admin_id:
                    await safe_send_message(context, owner_id, text)
                continue
            save_position_check(conn, analysis, check_source="auto")
            analyses_by_owner.setdefault(owner_id, []).append(analysis)

        owner_analyses = analyses_by_owner.get(admin_id, [])
        if owner_analyses:
            for message in format_full_report_messages(owner_analyses):
                await safe_send_message(context, admin_id, message, disable_web_page_preview=True)

        for user in active_authorized_users(conn):
            member_id = int(user["chat_id"])
            member_analyses = analyses_by_owner.get(member_id, [])
            if member_analyses:
                label = str(user["display_name"] or user["username"] or member_id)
                await safe_send_message(
                    context,
                    admin_id,
                    f"Автоотчет пользователя {label} (chat_id {member_id}):",
                )
            for message in format_full_report_messages(member_analyses):
                await safe_send_message(context, admin_id, message, disable_web_page_preview=True)
                await safe_send_message(context, member_id, message, disable_web_page_preview=True)

        await send_marketplace_overview(context, admin_id, "wb", notify_if_empty=False)
        await send_marketplace_overview(context, admin_id, "ym", notify_if_empty=False)
        for user in active_authorized_users(conn):
            member_id = int(user["chat_id"])
            if analyses_by_owner.get(member_id):
                await send_marketplace_overview(context, member_id, "wb", notify_if_empty=False)
                await send_marketplace_overview(context, member_id, "ym", notify_if_empty=False)


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
    conn = connect(config.database_path)
    admin_id = config.admin_chat_id or get_setting(conn, "admin_chat_id")
    if admin_id:
        claim_unowned_targets(conn, int(admin_id))
    conn.close()

    app.add_handler(MessageHandler(filters.COMMAND, audit_member_command), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("invite", invite_user))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("removeuser", remove_user))
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
