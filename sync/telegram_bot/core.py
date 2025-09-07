"""Telegram core primitives: API client, notifier, snapshot, and broadcasting."""

from __future__ import annotations

import json
import os
import urllib.error as _urlerr
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from utils import group_id_from_name

from .formatting import compute_schedule_diff, format_diff
from .store import get_store

# -------------------- API --------------------


class TelegramAPI:
    """Thin HTTP wrapper for Telegram Bot API using stdlib only."""

    def __init__(self, token: str, *, api_base: str = "https://api.telegram.org") -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")

    def call(self, method: str, params: dict | None = None, *, timeout: int = 25) -> dict:
        url = f"{self.api_base}/bot{self.token}/{method}"
        data = None
        headers = {"Content-Type": "application/json"}
        if params is not None:
            data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                return json.loads(payload)
        except urllib.error.HTTPError as e:  # pragma: no cover
            txt = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
            logger.exception("Telegram HTTP {} {}: {}", e.code, e.reason, txt)
            raise
        except urllib.error.URLError as e:  # pragma: no cover
            logger.exception("Ошибка Telegram API (URLError): {}", e)
            raise

    # Convenience wrappers
    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", params)

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 25,
        allowed_updates: list[str] | None = None,
    ) -> dict:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = allowed_updates
        return self.call("getUpdates", params, timeout=timeout + 5)


# -------------------- Notifier + persistence --------------------


def ensure_dir(path: str) -> None:
    d = path
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.warning("Не удалось прочитать JSON '{}': {}", path, e)
        return default


def write_json(path: str, obj) -> None:
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


@dataclass
class BotState:
    chats: list[int]
    last_update_id: int | None = None
    chat_groups: dict[str, str] | None = None  # chat_id -> group_id


class TelegramNotifier:
    """Broadcast + admin error reporting; persists subscribers in a JSON file."""

    def __init__(
        self,
        token: str,
        *,
        persist_dir: str = "sync/var/telegram",
        admin_user_id: int | None = None,
    ) -> None:
        self.api = TelegramAPI(token)
        self.persist_dir = persist_dir
        ensure_dir(persist_dir)
        self.state_path = os.path.join(persist_dir, "subscribers.json")
        raw = read_json(self.state_path, {"chats": [], "last_update_id": None, "chat_groups": {}})
        self.state = BotState(
            chats=list({int(c) for c in raw.get("chats", [])}),
            last_update_id=raw.get("last_update_id"),
            chat_groups={str(k): str(v) for k, v in (raw.get("chat_groups") or {}).items()},
        )
        # For private chats, Telegram chat id equals user id
        self.admin_user_id = admin_user_id

    # State helpers
    def save_state(self) -> None:
        write_json(
            self.state_path,
            {
                "chats": self.state.chats,
                "last_update_id": self.state.last_update_id,
                "chat_groups": self.state.chat_groups or {},
            },
        )

    def subscribe(self, chat_id: int) -> bool:
        if chat_id not in self.state.chats:
            self.state.chats.append(chat_id)
            self.save_state()
            return True
        return False

    def unsubscribe(self, chat_id: int) -> bool:
        if chat_id in self.state.chats:
            self.state.chats.remove(chat_id)
            self.save_state()
            return True
        return False

    # Messaging
    def send_to(self, chat_id: int, text: str, *, parse_mode: str | None = None) -> None:
        try:
            self.api.send_message(chat_id, text, parse_mode=parse_mode)
        except (_urlerr.HTTPError, _urlerr.URLError):
            logger.exception("Не удалось отправить сообщение в чат {}", chat_id)

    def broadcast(self, text: str, *, parse_mode: str | None = None) -> int:
        sent = 0
        for chat_id in list(self.state.chats):
            try:
                self.api.send_message(chat_id, text, parse_mode=parse_mode)
                sent += 1
            except (_urlerr.HTTPError, _urlerr.URLError):
                logger.exception("Не удалось отправить сообщение в чат {}", chat_id)
        return sent

    def send_error(self, text: str) -> None:
        if not self.admin_user_id:
            logger.debug("TELEGRAM_ADMIN_USER_ID не задан; пропускаем отправку ошибки")
            return
        self.send_to(self.admin_user_id, f"❗️ Ошибка:\n{text}")


# -------------------- Snapshots --------------------


def load_schedule_snapshot(path: str) -> list[Mapping[str, Any]]:
    obj = read_json(path, default=None)
    if not obj:
        return []
    if isinstance(obj, dict) and "items" in obj:
        return obj.get("items", [])
    if isinstance(obj, list):
        return obj
    return []


def save_schedule_snapshot(path: str, items: list[Mapping[str, Any]]) -> None:
    """Persist the latest parsed items with a generated_at timestamp (UTC ISO)."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    ts = _dt.now(_UTC).isoformat().replace("+00:00", "Z")
    obj = {"items": items, "generated_at": ts}
    write_json(path, obj)


# -------------------- Broadcast orchestrator --------------------


def _telegram_enabled(cfg) -> bool:
    try:
        return bool((cfg.telegram_enabled is None or cfg.telegram_enabled) and cfg.telegram_token)
    except Exception:
        return False


def _parse_start_dt(it: Mapping[str, Any], tz_name: str) -> datetime | None:
    """Return timezone-aware start datetime for an item or None if parsing fails."""
    d = (it.get("date") or "").strip()
    t = (it.get("start") or "").strip()
    if not d or not t:
        return None
    try:
        dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
    except Exception:
        return None
    try:
        from zoneinfo import ZoneInfo  # type: ignore

        return dt.replace(tzinfo=ZoneInfo(tz_name)) if tz_name else dt
    except Exception:
        return dt


def _filter_future_items(
    items: Iterable[Mapping[str, Any]], tz_name: str
) -> list[Mapping[str, Any]]:
    """Keep only items whose start time is strictly in the future in the given timezone."""
    try:
        from zoneinfo import ZoneInfo  # type: ignore

        now = datetime.now(ZoneInfo(tz_name)) if tz_name else datetime.now()
    except Exception:
        now = datetime.now()
    out: list[Mapping[str, Any]] = []
    for it in items:
        dt = _parse_start_dt(it, tz_name)
        if (dt is not None) and (dt > now):
            out.append(it)
    return out


def broadcast_diff_if_changes(
    cfg,
    prev_items: Iterable[Mapping[str, Any]],
    items: Iterable[Mapping[str, Any]],
    *,
    dry_run: bool,
) -> int | None:
    """If Telegram is enabled and not dry-run, compute diff and broadcast a summary."""

    if not _telegram_enabled(cfg) or dry_run:
        if dry_run:
            logger.debug("Dry-run: пропускаем отправку Telegram уведомлений")
        elif not getattr(cfg, "telegram_token", None):
            logger.debug("TELEGRAM_BOT_TOKEN не задан; Telegram уведомления отключены")
        return None

    tz_name = getattr(cfg, "timezone", "Europe/Moscow") or "Europe/Moscow"
    future_only = bool(getattr(cfg, "telegram_diff_future_only", True))
    prev_list = list(prev_items)
    curr_list = list(items)

    # Compute diffs for future-only view and for all items (for clearer logs)
    prev_scope = _filter_future_items(prev_list, tz_name) if future_only else prev_list
    curr_scope = _filter_future_items(curr_list, tz_name) if future_only else curr_list

    diff = compute_schedule_diff(prev_scope, curr_scope)
    changed_total = sum(len(diff[k]) for k in ("added", "removed", "modified"))
    # Always log a concise summary at DEBUG for diagnostics
    try:
        full_diff_summary = compute_schedule_diff(prev_list, curr_list)
        logger.debug(
            "Сравнение расписания: prev={}, curr={}, prev_scope={}, curr_scope={}, future_only={}",
            len(prev_list),
            len(curr_list),
            len(prev_scope),
            len(curr_scope),
            future_only,
        )
        logger.debug(
            "Итог различий: scope +{}, −{}, ✏️{}; все +{}, −{}, ✏️{}",
            len(diff["added"]),
            len(diff["removed"]),
            len(diff["modified"]),
            len(full_diff_summary["added"]),
            len(full_diff_summary["removed"]),
            len(full_diff_summary["modified"]),
        )
    except Exception:
        pass

    # If nothing changed within the chosen scope, clarify whether changes existed only in the past
    if changed_total <= 0:
        # Check overall changes to help diagnostics
        full_diff = compute_schedule_diff(prev_list, curr_list)
        full_changed = sum(len(full_diff[k]) for k in ("added", "removed", "modified"))
        if future_only and full_changed > 0:
            logger.info(
                "Изменений в будущих занятиях не обнаружено; были только изменения в прошедших. Уведомления не отправлялись"
            )
            # Provide detailed diff for diagnostics at DEBUG level
            try:
                from .formatting import format_diff as _fmt

                dbg = _fmt(full_diff, limit=1000)
                logger.debug("Детали изменений (прошедшие):\n{}", dbg)
            except Exception:
                pass
        else:
            logger.info("Изменений в расписании не обнаружено; уведомления не отправлялись")
        return None

    notifier = TelegramNotifier(
        cfg.telegram_token,
        persist_dir=cfg.telegram_persist_dir,
        admin_user_id=cfg.telegram_admin_user_id,
    )
    gid = group_id_from_name(getattr(cfg, "group", "") or "")
    header = f"{gid}: обновление расписания"
    body = format_diff(diff, limit=12)
    # Log detailed diff at DEBUG level
    try:
        dbg = format_diff(diff, limit=1000)
        scope_label = "будущие" if future_only else "все занятия"
        logger.debug("Изменения в расписании ({}):\n{}", scope_label, dbg)
    except Exception:
        pass
    msg = f"{header}\n\n{body}"

    # Determine recipients subscribed to this group
    store = None
    try:
        store = get_store(
            getattr(cfg, "database_url", None),
            notifier.state.chat_groups or {},
            notifier.state.chats,
        )
        subs = store.get_subscribers()
    except Exception:
        subs = list(notifier.state.chats)

    sent = 0
    for chat_id in subs:
        try:
            sel = None
            if store is not None:
                try:
                    sel = store.get_group(chat_id)
                except Exception:
                    pass
            if sel is None:
                sel = (notifier.state.chat_groups or {}).get(str(chat_id))
            if (sel or "").strip() == gid:
                notifier.api.send_message(chat_id, msg)
                sent += 1
        except Exception:
            logger.exception("Не удалось отправить сообщение в чат {}", chat_id)
    logger.info("Уведомления Telegram отправлены: {} получателей (группа {})", sent, gid)
    return sent
