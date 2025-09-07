from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv
from loguru import logger


class ConfigError(Exception):
    pass


BASE_URL = "https://cacs.philos.msu.ru/time-table/group?type=0"
MOSCOW_TZ = "Europe/Moscow"


def env_get(key: str, *aliases: str, default: str | None = None) -> str | None:
    """Return the first non-empty value from env among `key` and `aliases`."""

    for k in (key, *aliases):
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return v
    return default


def env_get_bool(key: str, *aliases: str, default: bool | None = None) -> bool | None:
    """Parse a boolean value from env for `key`/`aliases` if present."""

    v = env_get(key, *aliases)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class AppConfig:
    faculty: str
    course: str
    group: str
    groups: list[str] = field(default_factory=list)
    base_url: str = BASE_URL
    timezone: str = MOSCOW_TZ
    headless: bool | None = None
    dry_run: bool | None = None
    selenium_timeout: int = 20
    chrome_args: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    log_file: str | None = None
    log_color: bool | None = None
    group_languages: bool = True
    # Telegram
    telegram_enabled: bool | None = None
    telegram_token: str | None = None
    telegram_admin_user_id: int | None = None
    telegram_persist_dir: str = "sync/var/telegram"
    # Telegram webhook (public URL is required for webhook mode)
    telegram_webhook_url: str | None = None
    telegram_webhook_host: str = "0.0.0.0"
    telegram_webhook_port: int = 8081
    telegram_webhook_secret_token: str | None = None
    telegram_webhook_cert_path: str | None = None
    # Telegram broadcast behavior
    telegram_diff_future_only: bool = True
    # Local cache/snapshots
    schedule_snapshot_path: str = "sync/var/last_schedule.json"
    # Watch/refresh interval (seconds)
    watch_interval_seconds: int = 300
    # Simple timetable HTTP server (serves /timetable/<group_id>)
    serve_timetable: bool = True
    timetable_host: str = "127.0.0.1"
    timetable_port: int = 8080
    timetable_base_path: str = "/timetable"
    timetable_storage_dir: str = "sync/var/timetable"
    # Database (optional) for Telegram user data
    database_url: str | None = None


def load_env_config(env_path: str) -> AppConfig:
    """Load configuration from .env-style file and environment.

    Supports both Russian and English env keys for core fields.
    """

    # Try to load .env-style config from common locations
    def _try_load(paths: list[str]) -> bool:
        for p in paths:
            try:
                if p and os.path.isfile(p):
                    ok = load_dotenv(p)
                    if ok:
                        logger.debug("Загружен файл конфигурации: {}", p)
                        return True
            except Exception:
                continue
        return False

    candidates: list[str] = []
    # explicit path (absolute or relative to CWD)
    if env_path:
        if os.path.isabs(env_path):
            candidates.append(env_path)
        else:
            candidates.append(os.path.join(os.getcwd(), env_path))
            # also try next to this module
            candidates.append(os.path.join(os.path.dirname(__file__), env_path))
    # ENV_FILE override
    env_file_env = os.getenv("ENV_FILE")
    if env_file_env:
        candidates.insert(0, env_file_env)
    loaded_any = _try_load([p for p in candidates if p])
    if not loaded_any:
        # final fallback: try default behavior (may use current dir)
        load_dotenv(env_path)

    faculty = env_get("Факультет", "FACULTY")
    course = env_get("Курс", "COURSE")
    group = env_get("Группа", "GROUP")

    base_url = env_get("BASE_URL", default=BASE_URL)
    timezone = env_get("TIMEZONE", "TZ", default=MOSCOW_TZ)
    headless = env_get_bool("HEADLESS", default=None)
    dry_run = env_get_bool("DRY_RUN", default=None)
    timeout_raw = env_get("SELENIUM_TIMEOUT", default="20")
    try:
        selenium_timeout = int(timeout_raw) if timeout_raw is not None else 20
    except ValueError:
        selenium_timeout = 20
    chrome_args_raw = env_get("CHROME_ARGS")
    chrome_args = shlex.split(chrome_args_raw) if chrome_args_raw else []

    log_level = env_get("LOG_LEVEL", default="INFO")
    log_file = env_get("LOG_FILE")
    log_color = env_get_bool("LOG_COLOR", default=None)
    group_languages = env_get_bool("GROUP_LANGUAGES", default=True)

    # Telegram cfg
    telegram_enabled = env_get_bool("TELEGRAM_ENABLED", default=None)
    telegram_token = env_get("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN")
    # Prefer user id; keep chat/id aliases for backward compatibility
    admin_id_raw = env_get("TELEGRAM_ADMIN_USER_ID", "TELEGRAM_ADMIN_CHAT_ID", "TELEGRAM_ADMIN_ID")
    try:
        telegram_admin_user_id = int(admin_id_raw) if admin_id_raw else None
    except ValueError:
        telegram_admin_user_id = None
    telegram_persist_dir = env_get("TELEGRAM_PERSIST_DIR", default="sync/var/telegram")
    telegram_diff_future_only = env_get_bool("TELEGRAM_DIFF_FUTURE_ONLY", default=True)

    # Telegram webhook
    telegram_webhook_url = env_get("TELEGRAM_WEBHOOK_URL")
    telegram_webhook_host = env_get("TELEGRAM_WEBHOOK_HOST", default="0.0.0.0") or "0.0.0.0"
    try:
        telegram_webhook_port = int(env_get("TELEGRAM_WEBHOOK_PORT", default="8081") or "8081")
    except ValueError:
        telegram_webhook_port = 8081
    telegram_webhook_secret_token = env_get("TELEGRAM_WEBHOOK_SECRET_TOKEN")
    telegram_webhook_cert_path = env_get("TELEGRAM_WEBHOOK_CERT_PATH")

    # Snapshots/cache
    schedule_snapshot_path = env_get(
        "SCHEDULE_SNAPSHOT_PATH", default="sync/var/last_schedule.json"
    )

    # Watch interval: allow either seconds or minutes envs
    interval_sec = None
    interval_min = None
    interval_sec_raw = env_get("WATCH_INTERVAL_SECONDS", "POLL_INTERVAL_SECONDS")
    interval_min_raw = env_get("WATCH_INTERVAL_MINUTES", "POLL_INTERVAL_MINUTES")
    try:
        if interval_sec_raw is not None:
            interval_sec = int(str(interval_sec_raw).strip())
    except ValueError:
        interval_sec = None
    try:
        if interval_min_raw is not None:
            interval_min = int(str(interval_min_raw).strip())
    except ValueError:
        interval_min = None
    watch_interval_seconds = (
        interval_sec
        if (interval_sec is not None and interval_sec > 0)
        else (interval_min * 60 if (interval_min is not None and interval_min > 0) else 300)
    )

    # Multiple groups support (comma-separated)
    groups_raw = env_get("GROUPS", "ГРУППЫ")
    groups: list[str] = []
    if groups_raw:
        try:
            groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
        except Exception:
            groups = []
    # If multiple groups provided but single 'group' is missing, pick the first as default
    if (not group) and groups:
        group = groups[0]
    if not groups:
        groups = [group.strip()] if group else []

    if not (faculty and course and group):
        msg = "В .env.config должны быть заданы переменные: Факультет/Курс/Группа"
        logger.error(msg)
        raise ConfigError(msg)

    # Timetable server settings
    serve_timetable = env_get_bool("SERVE_TIMETABLE", default=True)
    timetable_host = env_get("TIMETABLE_HOST", default="127.0.0.1") or "127.0.0.1"
    try:
        timetable_port = int(env_get("TIMETABLE_PORT", default="8080") or "8080")
    except ValueError:
        timetable_port = 8080
    timetable_base_path = env_get("TIMETABLE_BASE_PATH", default="/timetable") or "/timetable"
    timetable_storage_dir = (
        env_get("TIMETABLE_STORAGE_DIR", default="sync/var/timetable") or "sync/var/timetable"
    )
    database_url = env_get("DATABASE_URL")

    return AppConfig(
        faculty=faculty.strip(),
        course=str(course).strip(),
        group=group.strip(),
        base_url=base_url,
        timezone=timezone,
        headless=headless,
        dry_run=dry_run,
        selenium_timeout=selenium_timeout,
        chrome_args=chrome_args,
        log_level=(log_level or "INFO").upper(),
        log_file=log_file,
        log_color=log_color,
        group_languages=bool(group_languages) if group_languages is not None else True,
        telegram_enabled=telegram_enabled,
        telegram_token=telegram_token,
        telegram_admin_user_id=telegram_admin_user_id,
        telegram_persist_dir=telegram_persist_dir,
        telegram_webhook_url=telegram_webhook_url,
        telegram_webhook_host=telegram_webhook_host,
        telegram_webhook_port=telegram_webhook_port,
        telegram_webhook_secret_token=telegram_webhook_secret_token,
        telegram_webhook_cert_path=telegram_webhook_cert_path,
        telegram_diff_future_only=True if telegram_diff_future_only is None else bool(telegram_diff_future_only),
        schedule_snapshot_path=schedule_snapshot_path,
        watch_interval_seconds=watch_interval_seconds,
        groups=groups,
        serve_timetable=True if serve_timetable is None else bool(serve_timetable),
        timetable_host=timetable_host,
        timetable_port=timetable_port,
        timetable_base_path=timetable_base_path,
        timetable_storage_dir=timetable_storage_dir,
        database_url=database_url,
    )


def setup_logging(
    level: str = "INFO", log_file: str | None = None, color: bool | None = None
) -> None:
    """Configure loguru sinks for console and optional file."""

    logger.remove()
    fmt_color = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>"
    )
    fmt_plain = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"
    )
    logger.add(
        sys.stderr,
        level=level,
        colorize=(True if color is None else bool(color)),
        backtrace=True,
        diagnose=False,
        format=fmt_color if (color is None or color) else fmt_plain,
    )
    if log_file:
        # Read rotation/retention/compression policy from env to control disk usage.
        # Defaults: rotate at 10 MB, keep only 1 day, compress as zip.
        rotation = env_get("LOG_ROTATION", default="10 MB") or "10 MB"
        retention = env_get("LOG_RETENTION", default="1 day") or "1 day"
        compression = env_get("LOG_COMPRESSION", default="zip") or "zip"
        try:
            d = os.path.dirname(log_file)
            if d and not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
        except Exception as e:
            logger.warning("Не удалось создать каталог для лога '{}': {}", log_file, e)
        logger.add(
            log_file,
            level=level,
            rotation=rotation,
            retention=retention,
            compression=compression,
            enqueue=True,
            backtrace=True,
            diagnose=False,
            format=fmt_plain,
        )
