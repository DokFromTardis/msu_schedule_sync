"""Telegram bot runtimes: long-polling and webhook server."""

from __future__ import annotations

import json
import os
import threading
import urllib.error as _urlerr
import urllib.request as _urlreq
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlparse

from loguru import logger

try:
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from utils import logged_sleep
from utils.config import AppConfig

from .core import TelegramAPI, TelegramNotifier, load_schedule_snapshot
from .formatting import (
    filter_items_by_date_range,
    format_day_message,
    format_ru_date,
    format_week_message,
)
from .store import BaseGroupStore, get_store


class TelegramBot:
    def __init__(
        self,
        token: str,
        *,
        persist_dir: str = "sync/var/telegram",
        admin_user_id: int | None = None,
        snapshot_path: str = "sync/var/last_schedule.json",
        timezone: str = "Europe/Moscow",
        timetable_storage_root: str = "sync/var/timetable",
        groups: list[str] | None = None,
    ) -> None:
        self.notifier = TelegramNotifier(
            token, persist_dir=persist_dir, admin_user_id=admin_user_id
        )
        self.api = self.notifier.api
        self.snapshot_path = snapshot_path
        self.tz = timezone
        self.storage_root = timetable_storage_root
        # known group ids for selection
        self.group_ids: list[str] = self._compute_group_ids(groups or [])
        # group store (DB or file)
        from os import getenv as _getenv

        db_url_env = _getenv("DATABASE_URL")
        self.store: BaseGroupStore = get_store(
            db_url_env, self.notifier.state.chat_groups or {}, self.notifier.state.chats
        )

    @staticmethod
    def _clean_group_id(name: str) -> str:
        import re as _re

        s = (name or "").strip()
        m = _re.match(r"^(\d+)", s)
        if m:
            return m.group(1)
        digits = "".join(ch for ch in s if ch.isdigit())
        return digits or s

    def _compute_group_ids(self, group_names: list[str]) -> list[str]:
        ids = []
        for g in group_names:
            gid = self._clean_group_id(g)
            if gid and gid not in ids:
                ids.append(gid)
        return ids

    # Keyboards
    @property
    def main_kb(self) -> dict:
        return {
            "keyboard": [
                ["Сегодня", "Завтра"],
                ["Эта неделя", "Следующая неделя"],
                ["Сменить группу"],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }

    @property
    def group_kb(self) -> dict:
        gids = self.group_ids or []
        rows: list[list[str]] = []
        row: list[str] = []
        for gid in gids:
            row.append(gid)
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(["Назад"])
        return {"keyboard": rows, "resize_keyboard": True}

    @staticmethod
    def _help_text() -> str:
        return (
            "📅 Бот расписания МГУ\n\n"
            "Что могу:\n"
            "• Показать занятия на Сегодня/Завтра\n"
            "• Сформировать недельный обзор (Эта/Следующая неделя)\n"
            "• Присылать уведомления об изменениях\n\n"
            "Команды:\n"
            "/subscribe — подписаться на уведомления\n"
            "/unsubscribe — отменить уведомления\n"
            "/today — расписание на сегодня\n"
            "/week — расписание на текущую неделю\n"
            "/nextweek — расписание на следующую неделю\n"
            "/help — показать помощь\n\n"
            "Чтобы сменить группу — используйте кнопку «Сменить группу»."
        )

    @staticmethod
    def _welcome_text(gid: str) -> str:
        return (
            "👋 Добро пожаловать!\n\n"
            "Этот бот помогает быстро смотреть расписание и получать уведомления об изменениях.\n\n"
            "Доступно с кнопок ниже:\n"
            "• Сегодня / Завтра\n"
            "• Эта неделя / Следующая неделя\n"
            "• Сменить группу\n\n"
            f"Текущая группа: <b>{gid}</b>\n"
            "Уведомления об изменениях включены по умолчанию."
        )

    def _current_group_id(self, chat_id: int) -> str:
        gid = self.store.get_group(chat_id)
        if gid:
            return gid
        if "104" in (self.group_ids or []):
            return "104"
        return self.group_ids[0] if self.group_ids else "104"

    def _set_group_for_chat(self, chat_id: int, gid: str) -> None:
        try:
            self.store.set_group(chat_id, gid)
        finally:
            if not self.notifier.state.chat_groups:
                self.notifier.state.chat_groups = {}
            self.notifier.state.chat_groups[str(chat_id)] = gid
            self.notifier.save_state()

    def _load_items(self, *, chat_id: int | None = None):
        if chat_id is None:
            return load_schedule_snapshot(self.snapshot_path)
        gid = self._current_group_id(chat_id)
        path = f"{self.storage_root}/{gid}/last_schedule.json"
        items = load_schedule_snapshot(path)
        if items:
            return items
        return load_schedule_snapshot(self.snapshot_path)

    def _today_range(self) -> tuple[date, date]:
        if ZoneInfo and self.tz:
            try:
                tz = ZoneInfo(self.tz)
                now = datetime.now(tz)
            except Exception:
                now = datetime.now()
        else:
            now = datetime.now()
        d = now.date()
        return d, d

    def _week_range(self, *, next_week: bool = False) -> tuple[date, date]:
        today = self._today_range()[0]
        monday = today - timedelta(days=today.weekday())
        if next_week:
            monday = monday + timedelta(days=7)
        sunday = monday + timedelta(days=6)
        return monday, sunday

    def _handle_command(self, chat_id: int, text: str) -> None:
        try:
            try:
                self.store.add_subscriber(chat_id)
            except Exception:
                pass
            self.notifier.subscribe(chat_id)
        except Exception:
            pass

        t = text.strip().lower()
        if t == "/start":
            gid = self._current_group_id(chat_id)
            msg = self._welcome_text(gid)
            try:
                self.api.send_message(chat_id, msg, parse_mode="HTML", reply_markup=self.main_kb)
            except Exception:
                self.notifier.send_to(chat_id, msg)
            return
        if t == "/help":
            gid = self._current_group_id(chat_id)
            msg = self._help_text() + f"\n\nТекущая группа: {gid}"
            try:
                self.api.send_message(chat_id, msg, reply_markup=self.main_kb)
            except Exception:
                self.notifier.send_to(chat_id, msg)
            return

        if t == "сегодня" or t == "/today":
            gid = self._current_group_id(chat_id)
            items = self._load_items(chat_id=chat_id)
            start, end = self._today_range()
            todays = filter_items_by_date_range(items, start=start, end=end)
            header_title = f"Группа {gid} — Сегодня"
            header_span = format_ru_date(start)
            msg = format_day_message(todays, header_title=header_title, header_span=header_span)
            self.api.send_message(chat_id, msg, parse_mode="HTML", reply_markup=self.main_kb)
            return
        if t == "завтра" or t == "/tomorrow":
            gid = self._current_group_id(chat_id)
            items = self._load_items(chat_id=chat_id)
            today, _ = self._today_range()
            from datetime import timedelta as _td

            d = today + _td(days=1)
            tomorrows = filter_items_by_date_range(items, start=d, end=d)
            header_title = f"Группа {gid} — Завтра"
            header_span = format_ru_date(d)
            msg = format_day_message(tomorrows, header_title=header_title, header_span=header_span)
            self.api.send_message(chat_id, msg, parse_mode="HTML", reply_markup=self.main_kb)
            return
        if t in ("эта неделя", "/week"):
            gid = self._current_group_id(chat_id)
            items = self._load_items(chat_id=chat_id)
            start, end = self._week_range(next_week=False)
            weekly = filter_items_by_date_range(items, start=start, end=end)
            header_title = f"Группа {gid} — Неделя {format_ru_date(start).split()[1]}"
            header_span = f"{start.day}–{end.day} {format_ru_date(end).split()[1]}"
            msg = format_week_message(weekly, header_title=header_title, header_span=header_span)
            self.api.send_message(chat_id, msg, parse_mode="HTML", reply_markup=self.main_kb)
            return
        if t in ("следующая неделя", "/nextweek"):
            gid = self._current_group_id(chat_id)
            items = self._load_items(chat_id=chat_id)
            start, end = self._week_range(next_week=True)
            weekly = filter_items_by_date_range(items, start=start, end=end)
            header_title = f"Группа {gid} — Следующая неделя {format_ru_date(start).split()[1]}"
            header_span = f"{start.day}–{end.day} {format_ru_date(end).split()[1]}"
            msg = format_week_message(weekly, header_title=header_title, header_span=header_span)
            self.api.send_message(chat_id, msg, parse_mode="HTML", reply_markup=self.main_kb)
            return
        if t == "сменить группу":
            self.api.send_message(chat_id, "Выберите группу:", reply_markup=self.group_kb)
            return
        if t.isdigit() and len(t) in (3, 4):
            gid = t
            if gid in self.group_ids:
                self._set_group_for_chat(chat_id, gid)
                self.api.send_message(
                    chat_id, f"Группа установлена: {gid}", reply_markup=self.main_kb
                )
                return
        if t == "назад":
            self.api.send_message(chat_id, "Главное меню", reply_markup=self.main_kb)
            return
        if t == "/subscribe":
            added_db = False
            try:
                added_db = self.store.add_subscriber(chat_id)
            except Exception:
                pass
            added_file = self.notifier.subscribe(chat_id)
            self.notifier.send_to(
                chat_id,
                "Вы подписаны на уведомления." if (added_db or added_file) else "Вы уже подписаны.",
            )
            return
        if t == "/unsubscribe":
            removed_db = False
            try:
                removed_db = self.store.remove_subscriber(chat_id)
            except Exception:
                pass
            removed_file = self.notifier.unsubscribe(chat_id)
            self.notifier.send_to(
                chat_id,
                "Подписка отменена." if (removed_db or removed_file) else "Вы и так не подписаны.",
            )
            return
        self.notifier.send_to(chat_id, "Неизвестная команда. /help для списка команд.")

    # Public wrapper for command handling
    def handle_text_message(self, chat_id: int, text: str) -> None:
        self._handle_command(chat_id, text)

    # Long-polling loop
    def poll_forever(self, *, long_poll_timeout: int = 25, sleep_on_error: int = 3) -> None:
        logger.info("Запуск Telegram бота (long polling)…")
        try:
            self.api.call("deleteWebhook", {"drop_pending_updates": False})
            logger.debug("Webhook отключен (deleteWebhook)")
        except Exception:
            logger.debug("Не удалось отключить webhook; продолжаем long polling")
        offset = self.notifier.state.last_update_id or None
        fail_streak = 0
        while True:
            try:
                res = self.api.get_updates(
                    offset=offset, timeout=long_poll_timeout, allowed_updates=["message"]
                )
                if not res.get("ok"):
                    logger.warning("Telegram getUpdates ответ: {}", res)
                    logged_sleep(sleep_on_error, message="Пауза после ответа Telegram")
                    continue
                for upd in res.get("result", []):
                    offset = upd.get("update_id", 0) + 1
                    self.notifier.state.last_update_id = offset
                    self.notifier.save_state()
                    msg = upd.get("message") or {}
                    chat = msg.get("chat") or {}
                    chat_id = chat.get("id")
                    text = msg.get("text") or ""
                    if not chat_id or not text:
                        continue
                    self._handle_command(int(chat_id), text)
                fail_streak = 0
            except KeyboardInterrupt:  # pragma: no cover
                logger.info("Остановка Telegram бота по Ctrl+C")
                break
            except (_urlerr.HTTPError, _urlerr.URLError, ValueError) as e:
                fail_streak += 1
                backoff = min(60, sleep_on_error * (2 ** min(fail_streak, 3)))
                logger.warning(
                    "Сбой long polling: {}. Повтор через {} c",
                    str(e).splitlines()[0],
                    backoff,
                )
                logged_sleep(backoff, message="Пауза после ошибки long polling")
            except Exception as e:
                fail_streak += 1
                backoff = min(60, sleep_on_error * (2 ** min(fail_streak, 3)))
                logger.warning(
                    "Неожиданная ошибка long polling: {}. Повтор через {} c",
                    str(e).splitlines()[0],
                    backoff,
                )
                logged_sleep(backoff, message="Пауза после ошибки long polling")


def start_bot_background(
    cfg: AppConfig,
    *,
    long_poll_timeout: int = 25,
    sleep_on_error: int = 3,
) -> threading.Thread | None:
    """Start Telegram bot in a daemon thread if enabled; return the thread or None."""
    try:
        tel_enabled = bool(
            ((cfg.telegram_enabled is None) or cfg.telegram_enabled) and cfg.telegram_token
        )
        if not tel_enabled:
            if not cfg.telegram_token:
                logger.debug("TELEGRAM_BOT_TOKEN не задан; запуск Telegram бота пропущен")
            if cfg.telegram_enabled is False:
                logger.debug("TELEGRAM_ENABLED=false; запуск Telegram бота отключён")
            return None
        bot = TelegramBot(
            cfg.telegram_token,
            persist_dir=cfg.telegram_persist_dir,
            admin_user_id=cfg.telegram_admin_user_id,
            snapshot_path=cfg.schedule_snapshot_path,
            timezone=cfg.timezone,
            timetable_storage_root=getattr(cfg, "timetable_storage_dir", "sync/var/timetable"),
            groups=getattr(cfg, "groups", [cfg.group]),
        )
        t = threading.Thread(
            target=bot.poll_forever,
            kwargs={"long_poll_timeout": long_poll_timeout, "sleep_on_error": sleep_on_error},
            daemon=True,
        )
        t.start()
        logger.info("Телеграм-бот запущен в фоне (long polling)…")
        return t
    except Exception:
        logger.exception("Не удалось запустить Telegram бота в фоне")
        return None


# -------------------- Webhook server --------------------


class _WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler that processes Telegram webhook updates.

    Class attributes are assigned by the factory in start_webhook_background:
    - bot: TelegramBot
    - path: str (expected path for webhook)
    - secret_token: str | None (expected header value)
    """

    bot: TelegramBot
    path: str
    secret_token: str | None

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logger.debug("TelegramWebhook: " + format, *args)

    def _send(
        self, code: int, *, body: bytes | None = None, content_type: str = "application/json"
    ) -> None:
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Content-Length", "0")
        self.end_headers()
        if body is not None:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/healthz", "/readyz"):
            self._send(200, body=b"ok", content_type="text/plain; charset=utf-8")
            return
        parsed = urlparse(self.path)
        if parsed.path == self.path_expected():
            self._send(200, body=b"ok", content_type="text/plain; charset=utf-8")
            return
        self._send(404, body=b"not found", content_type="text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != self.path_expected():
            self._send(404, body=b"not found", content_type="text/plain; charset=utf-8")
            return

        if self.secret_token:
            header_val = (self.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
            if header_val != self.secret_token:
                logger.warning("Webhook: secret token mismatch")
                self._send(401, body=b"unauthorized", content_type="text/plain; charset=utf-8")
                return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except Exception:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            logger.warning("Webhook: invalid JSON payload")
            self._send(400, body=b"bad request", content_type="text/plain; charset=utf-8")
            return

        msg = payload.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = msg.get("text") or ""
        if not chat_id or not text:
            self._send(200, body=b'{"ok":true}')
            return

        try:
            self.bot.handle_text_message(int(chat_id), text)
        except Exception:
            logger.exception("Webhook: failed to process message")
        self._send(200, body=b'{"ok":true}')

    @classmethod
    def path_expected(cls) -> str:
        p = cls.path or "/"
        return p if p.startswith("/") else "/" + p


def _extract_path_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).path
    except Exception:
        return None


def start_webhook_background(cfg: AppConfig) -> threading.Thread | None:
    """Start Telegram webhook HTTP server and configure setWebhook.

    Returns the daemon thread or None if Telegram is disabled or misconfigured.
    """
    try:
        tel_enabled = bool(
            ((cfg.telegram_enabled is None) or cfg.telegram_enabled) and cfg.telegram_token
        )
        if not tel_enabled:
            if not cfg.telegram_token:
                logger.debug("TELEGRAM_BOT_TOKEN не задан; запуск Telegram вебхука пропущен")
            if cfg.telegram_enabled is False:
                logger.debug("TELEGRAM_ENABLED=false; запуск Telegram вебхука отключён")
            return None

        webhook_url: str | None = getattr(cfg, "telegram_webhook_url", None)
        if not webhook_url:
            logger.error("TELEGRAM_WEBHOOK_URL не задан; невозможно настроить вебхук Telegram")
            return None

        host: str = getattr(cfg, "telegram_webhook_host", "0.0.0.0") or "0.0.0.0"
        try:
            port: int = int(getattr(cfg, "telegram_webhook_port", 8081) or 8081)
        except Exception:
            port = 8081
        path = _extract_path_from_url(webhook_url) or "/telegram"

        bot = TelegramBot(
            cfg.telegram_token,  # type: ignore[arg-type]
            persist_dir=cfg.telegram_persist_dir,
            admin_user_id=cfg.telegram_admin_user_id,
            snapshot_path=cfg.schedule_snapshot_path,
            timezone=cfg.timezone,
            timetable_storage_root=getattr(cfg, "timetable_storage_dir", "sync/var/timetable"),
            groups=getattr(cfg, "groups", [cfg.group]),
        )

        _WebhookHandler.bot = bot
        _WebhookHandler.path = path
        secret_token: str | None = getattr(cfg, "telegram_webhook_secret_token", None)
        _WebhookHandler.secret_token = secret_token

        httpd = ThreadingHTTPServer(
            (host, port), cast(type[BaseHTTPRequestHandler], _WebhookHandler)
        )

        def _run():
            logger.info(
                "Telegram webhook сервер: http://{}:{}{} (public: {})",
                host,
                port,
                path,
                webhook_url,
            )
            try:
                httpd.serve_forever(poll_interval=0.5)
            except Exception as e:
                logger.exception("Сбой Telegram webhook сервера: {}", e)
            finally:
                try:
                    httpd.server_close()
                except Exception:
                    pass

        t = threading.Thread(target=_run, name="telegram-webhook", daemon=True)
        t.start()

        api = TelegramAPI(cfg.telegram_token)  # type: ignore[arg-type]
        cert_path: str | None = getattr(cfg, "telegram_webhook_cert_path", None)
        try:
            if cert_path and os.path.isfile(cert_path):
                _set_webhook_with_certificate(
                    api, webhook_url, secret_token=secret_token, cert_path=cert_path
                )
            else:
                params: dict[str, Any] = {
                    "url": webhook_url,
                    "allowed_updates": ["message"],
                    "drop_pending_updates": False,
                }
                if secret_token:
                    params["secret_token"] = secret_token
                res = api.call("setWebhook", params)
                if not res.get("ok"):
                    logger.warning("Telegram setWebhook ответ: {}", res)
                else:
                    logger.info("Webhook Telegram настроен")
        except Exception:
            logger.exception(
                "Не удалось выполнить setWebhook; проверьте доступность {}", webhook_url
            )

        return t
    except Exception:
        logger.exception("Не удалось запустить Telegram webhook")
        return None


def _encode_multipart(
    fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
) -> tuple[bytes, str]:
    """Build a minimal multipart/form-data body."""
    import uuid

    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    sep = ("--" + boundary + "\r\n").encode("utf-8")
    end = ("--" + boundary + "--\r\n").encode("utf-8")
    out = bytearray()
    for k, v in fields.items():
        out += sep
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += (str(v) + "\r\n").encode("utf-8")
    for k, (filename, content, ctype) in files.items():
        out += sep
        out += (f'Content-Disposition: form-data; name="{k}"; filename="{filename}"\r\n').encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += content + b"\r\n"
    out += end
    ctype_header = f"multipart/form-data; boundary={boundary}"
    return bytes(out), ctype_header


def _set_webhook_with_certificate(
    api: TelegramAPI, url: str, *, secret_token: str | None, cert_path: str
) -> None:
    """Call setWebhook with self-signed certificate via multipart upload."""

    endpoint = f"{api.api_base}/bot{api.token}/setWebhook"
    with open(cert_path, "rb") as f:
        cert_bytes = f.read()
    fields = {
        "url": url,
        "allowed_updates": '["message"]',
        "drop_pending_updates": "false",
    }
    if secret_token:
        fields["secret_token"] = secret_token
    files = {"certificate": (os.path.basename(cert_path), cert_bytes, "application/octet-stream")}
    body, ctype = _encode_multipart(fields, files)
    req = _urlreq.Request(endpoint, data=body, headers={"Content-Type": ctype})
    with _urlreq.urlopen(req, timeout=30) as resp:
        payload = resp.read()
    try:
        res = json.loads(payload)
    except Exception:
        res = {"ok": False, "result": payload.decode("utf-8", errors="ignore")}
    if not res.get("ok"):
        logger.warning("Telegram setWebhook (multipart) ответ: {}", res)
    else:
        logger.info("Webhook Telegram настроен (с самоподписанным сертификатом)")
