from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    telegram_token: str
    admin_chat_id: int | None
    setup_key: str
    database_path: str
    timezone: ZoneInfo
    report_times: tuple[str, ...]
    report_interval_days: int
    wb_dest: str
    wb_currency: str
    wb_locale: str
    wb_max_search_pages: int
    wb_request_delay_seconds: float
    wb_request_delay_jitter_seconds: float
    wb_request_retries: int
    wb_429_cooldown_seconds: float
    wb_proxy_url: str
    wb_proxy_auth_token: str
    wb_proxy_insecure_ssl: bool
    request_timeout: float


def _chat_id(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Kyiv")


def _report_times(value: str) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in value.split(","):
        item = item.strip()
        if not item or item in seen or ":" not in item:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(os.getenv(name, str(default)) or default), minimum)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_config(require_telegram: bool = True) -> Config:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if require_telegram and not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    times = _report_times(os.getenv("REPORT_TIMES", "09:00"))

    return Config(
        telegram_token=token,
        admin_chat_id=_chat_id(os.getenv("ADMIN_CHAT_ID")),
        setup_key=os.getenv("SETUP_KEY", "change-me").strip(),
        database_path=os.getenv("DATABASE_PATH", "data/wb_position_bot.db"),
        timezone=_timezone(os.getenv("TIMEZONE", "Europe/Kyiv")),
        report_times=times or ("09:00",),
        report_interval_days=_int_env("REPORT_INTERVAL_DAYS", 1, minimum=1),
        wb_dest=os.getenv("WB_DEST", "-1257786").strip(),
        wb_currency=os.getenv("WB_CURRENCY", "rub").strip(),
        wb_locale=os.getenv("WB_LOCALE", "ru").strip(),
        wb_max_search_pages=_int_env("WB_MAX_SEARCH_PAGES", 20, minimum=1),
        wb_request_delay_seconds=_float_env("WB_REQUEST_DELAY_SECONDS", 2.0),
        wb_request_delay_jitter_seconds=_float_env("WB_REQUEST_DELAY_JITTER_SECONDS", 3.0),
        wb_request_retries=_int_env("WB_REQUEST_RETRIES", 2, minimum=1),
        wb_429_cooldown_seconds=_float_env("WB_429_COOLDOWN_SECONDS", 15.0),
        wb_proxy_url=os.getenv("WB_PROXY_URL", "").strip(),
        wb_proxy_auth_token=os.getenv("WB_PROXY_AUTH_TOKEN", "").strip(),
        wb_proxy_insecure_ssl=_bool_env("WB_PROXY_INSECURE_SSL", False),
        request_timeout=_float_env("REQUEST_TIMEOUT", 60.0),
    )
