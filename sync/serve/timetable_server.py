"""Lightweight HTTP server to serve per‑group ICS calendars.

Endpoints:
- GET /timetable/<group_id>            → returns text/calendar (aggregated calendar)
- GET /timetable/<group_id>.ics        → same as above

Storage layout on disk (read by the server and written by the publisher):
  <storage_root>/<group_id>/calendar.ics

No authentication or CalDAV methods are implemented.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from zoneinfo import ZoneInfo

from loguru import logger

from utils import group_id_from_name, stable_event_key

try:  # optional but recommended
    from icalendar import Calendar, Event  # type: ignore
except Exception:  # pragma: no cover - import guard
    Calendar = None  # type: ignore
    Event = None  # type: ignore

# Use Telegram formatting helpers for language bullets (module is part of repo)
from sync.telegram_bot.formatting import build_language_bullets


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _calendar_path(storage_root: str, group_id: str) -> str:
    return os.path.join(storage_root, group_id, "calendar.ics")


def _build_calendar_ics(items: list[Mapping[str, Any]], *, tz: str) -> bytes:
    """Build a VCALENDAR containing one VEVENT per item.

    Falls back to a minimal textual representation if icalendar is unavailable.
    """

    def _pretty_description_lines(it: Mapping[str, Any]) -> list[str]:
        """Return pretty, Telegram-like plain-text description lines for ICS."""
        out: list[str] = []
        start = (it.get("start") or "").strip()
        end = (it.get("end") or "").strip()
        time_span = f"{start}–{end}".strip("–") if start or end else ""
        pair_no = it.get("pair")
        pair_label = it.get("pair_label") or (f"{pair_no} пара" if pair_no else None)
        label = pair_label or "Пара"
        # Detect language-grouped title via build_language_bullets
        lang_lines = build_language_bullets(it.get("title", ""))
        if len(lang_lines) >= 2:
            # Header with time, then language bullets, then meta
            header = f"- ⏰ {label} ({time_span})" if time_span else f"- ⏰ {label}"
            out.append(header)
            out.extend(lang_lines)
            teacher = (it.get("teacher") or "").strip()
            if teacher:
                out.append(f"- 🧑‍🏫 Преподаватель: {teacher}")
            return out
        # Regular lesson
        header = f"- ⏰ {label} ({time_span})" if time_span else f"- ⏰ {label}"
        out.append(header)
        title = (it.get("title") or "").strip()
        if title:
            out.append(f"- 📚 {title}")
        room = (it.get("room") or "").strip()
        if room:
            out.append(f"- 📍 Аудитория: {room}")
        teacher = (it.get("teacher") or "").strip()
        if teacher:
            out.append(f"- 🧑‍🏫 Преподаватель: {teacher}")
        # Suppress group info and added-at in ICS description as requested
        return out

    def _summary_with_room(it: Mapping[str, Any]) -> str:
        """Return ICS SUMMARY label.

        - Default: room before subject, e.g. "[B1] Философия".
        - Languages (English/German/French): constant label "🇬🇧🇩🇪🇫🇷 Иностранный язык" with no room.
        """
        title = (str(it.get("title")) or "").strip()
        room = (str(it.get("room")) or "").strip()
        try:
            import re as _re

            if _re.search(r"\b(Английский|Немецкий|Французский)\b", title):
                return "🇬🇧🇩🇪🇫🇷 Иностранный язык"
        except Exception:
            pass
        return f"[{room}] {title}" if room else title

    if Calendar is None or Event is None:  # fallback
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MSU Timetable//ru//"]
        for it in items:
            uid = make_event_uid(it)
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:{uid}",
                    f"SUMMARY:{_summary_with_room(it)}",
                ]
            )
            if it.get("room"):
                lines.append(f"LOCATION:{it.get('room')}")
            dt0 = f"{str(it.get('date','1970-01-01')).replace('-','')}T{str(it.get('start') or '00:00').replace(':','')}00"
            dt1 = f"{str(it.get('date','1970-01-01')).replace('-','')}T{str(it.get('end') or '00:00').replace(':','')}00"
            lines.append(f"DTSTART:{dt0}")
            lines.append(f"DTEND:{dt1}")
            desc_parts = _pretty_description_lines(it)
            if desc_parts:
                desc = "\\n".join(desc_parts).replace("\n", "\\n")
                lines.append(f"DESCRIPTION:{desc}")
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        return ("\n".join(lines) + "\n").encode("utf-8")

    # Proper icalendar
    from datetime import datetime
    from zoneinfo import ZoneInfo

    cal = Calendar()
    cal.add("prodid", "-//MSU Timetable//ru//")
    cal.add("version", "2.0")

    for it in items:
        ev = Event()
        ev.add("uid", make_event_uid(it))
        ev.add("summary", _summary_with_room(it))
        if it.get("room"):
            ev.add("location", it.get("room"))

        # Localized times
        try:
            hh, mm = str(it.get("start") or "00:00").split(":")
            dd, ee = str(it.get("end") or "00:00").split(":")
            dt0 = datetime.fromisoformat(
                f"{it.get('date','1970-01-01')}T{int(hh):02d}:{int(mm):02d}:00"
            ).replace(tzinfo=ZoneInfo(tz))
            dt1 = datetime.fromisoformat(
                f"{it.get('date','1970-01-01')}T{int(dd):02d}:{int(ee):02d}:00"
            ).replace(tzinfo=ZoneInfo(tz))
            ev.add("dtstart", dt0)
            ev.add("dtend", dt1)
        except Exception:
            pass

        desc_lines: list[str] = _pretty_description_lines(it)
        if desc_lines:
            ev.add("description", "\n".join(desc_lines))

        from datetime import UTC as _UTC
        from datetime import datetime as _dtu

        ev.add("dtstamp", _dtu.now(_UTC))
        ev.add("transp", "OPAQUE")
        ev.add("status", "CONFIRMED")
        cal.add_component(ev)

    return cal.to_ical()


def make_event_uid(item: Mapping[str, Any]) -> str:
    """Deterministic UID for stability across runs."""
    key = stable_event_key(item)
    return "msu_" + hashlib.md5(key.encode("utf-8")).hexdigest()


def write_group_calendar_fs(
    items: list[Mapping[str, Any]],
    *,
    storage_root: str,
    group_id: str,
    timezone: str,
) -> tuple[int, bool]:
    """Write aggregated calendar.ics for the group. Returns (event_count, changed)."""

    dir_path = os.path.join(storage_root, group_id)
    _ensure_dir(dir_path)
    data = _build_calendar_ics(items, tz=timezone)
    fp = _calendar_path(storage_root, group_id)
    prev = b""
    try:
        prev = _read_file(fp)
    except FileNotFoundError:
        prev = b""
    except Exception:
        prev = b""
    changed = prev != data
    tmp = fp + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, fp)
    return len(items), changed


def _collect_landing_rows(storage_root: str, *, display_tz: str) -> list[tuple[str, int, str]]:
    """Collect (group_id, count, updated_fmt) for landing page.

    updated time is converted to the provided display timezone and labeled 'МСК' if
    Europe/Moscow, otherwise the IANA key is shown.
    """
    rows: list[tuple[str, int, str]] = []
    try:
        entries = sorted(
            [d for d in os.listdir(storage_root) if os.path.isdir(os.path.join(storage_root, d))]
        )
    except Exception:
        entries = []
    for d in entries:
        ics_path = os.path.join(storage_root, d, "calendar.ics")
        if not os.path.isfile(ics_path):
            continue
        cnt = 0
        updated = ""
        snap_path = os.path.join(storage_root, d, "last_schedule.json")
        try:
            with open(snap_path, encoding="utf-8") as f:
                snap = json.load(f)
            items = snap.get("items") or []
            cnt = len(items)
            ts = (snap.get("generated_at") or "").strip()
            if ts:
                try:
                    dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt_utc.tzinfo is None:
                        dt_utc = dt_utc.replace(tzinfo=ZoneInfo("UTC"))
                    tz = ZoneInfo(display_tz or "Europe/Moscow")
                    dt_loc = dt_utc.astimezone(tz)
                    label = "МСК" if display_tz == "Europe/Moscow" else display_tz
                    updated = dt_loc.strftime("%Y-%m-%d %H:%M ") + (label or "")
                except Exception:
                    updated = ts
        except Exception:
            # Fallback: filesystem mtime of ICS (UTC → local tz)
            try:
                mt = os.path.getmtime(ics_path)
                dt_utc = datetime.fromtimestamp(mt, UTC)
                tz = ZoneInfo(display_tz or "Europe/Moscow")
                dt_loc = dt_utc.astimezone(tz)
                label = "МСК" if display_tz == "Europe/Moscow" else display_tz
                updated = dt_loc.strftime("%Y-%m-%d %H:%M ") + (label or "")
            except Exception:
                updated = ""
        rows.append((d, cnt, updated))
    return rows


class _TimetableHandler(BaseHTTPRequestHandler):
    server_version = "msu-timetable/0.1"

    # set by factory
    base_path: str
    storage_root: str
    display_tz: str

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logger.debug("HTTP: " + format, *args)

    def _send(
        self, code: int, *, headers: dict[str, str] | None = None, body: bytes | None = None
    ) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if body is not None:
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Content-Length", "0")
        self.end_headers()
        if body is not None:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = (self.path or "/").split("?", 1)[0]
        base = self.base_path.rstrip("/") or "/timetable"
        if path.rstrip("/") == base:
            # Landing page: list available group links with counts and last updated
            logger.debug("Формируем главную страницу расписания ({})", self.storage_root)
            # Optional groups.json mapping (id->name)
            name_map: dict[str, str] = {}
            try:
                with open(os.path.join(self.storage_root, "groups.json"), encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data.get("groups") or []:
                    gid = str(entry.get("id") or "").strip()
                    name = str(entry.get("name") or "").strip()
                    if gid and name:
                        name_map[gid] = name
            except Exception:
                pass
            rows = _collect_landing_rows(self.storage_root, display_tz=self.display_tz)

            html = [
                "<!doctype html>",
                '<html lang="ru">',
                '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
                "<title>Расписание — группы</title>",
                "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:24px;max-width:900px;margin:0 auto} a{color:#0366d6;text-decoration:none} .card{border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(0,0,0,0.03)} .meta{color:#6b7280;font-size:14px;margin-top:8px}</style>",
                "</head><body>",
                "<h1>Доступные группы</h1>",
            ]
            if not rows:
                html.append("<p>Нет опубликованных групп (ожидаются файлы calendar.ics).</p>")
            else:
                for gid, cnt, updated in rows:
                    href = f"{base}/{gid}"
                    title = f"Группа {gid}"
                    subtitle = name_map.get(gid)
                    html.append('<div class="card">')
                    html.append(f'<div><a href="{href}"><strong>{title}</strong></a></div>')
                    if subtitle:
                        html.append(f'<div class="meta">{subtitle}</div>')
                    html.append(
                        f"<div class=\"meta\">Занятий: {cnt}{' · ' + updated if updated else ''}</div>"
                    )
                    html.append("</div>")
            html.extend(["</body></html>"])
            body = "\n".join(html).encode("utf-8")
            self._send(200, headers={"Content-Type": "text/html; charset=utf-8"}, body=body)
            return

        if path.startswith(base.rstrip("/") + "/"):
            rel = path[len(base.rstrip("/") + "/") :]
            gid = rel
            if gid.endswith(".ics"):
                gid = gid[:-4]
            gid = gid.strip("/")
            file_path = _calendar_path(self.storage_root, gid)
            if os.path.isfile(file_path):
                try:
                    data = _read_file(file_path)
                except Exception:
                    self._send(
                        500,
                        headers={"Content-Type": "text/plain; charset=utf-8"},
                        body=b"read error",
                    )
                    return
                etag = '"' + _md5_hex(data) + '"'
                inm = (self.headers.get("If-None-Match") or "").strip()
                if inm and inm == etag:
                    self._send(304, headers={"ETag": etag}, body=None)
                else:
                    headers = {"Content-Type": "text/calendar; charset=utf-8", "ETag": etag}
                    self._send(200, headers=headers, body=data)
                logger.info("Выдан календарь группы {} ({} байт)", gid, len(data))
                return
            self._send(
                404, headers={"Content-Type": "text/plain; charset=utf-8"}, body=b"not found"
            )
            return

        self._send(404, headers={"Content-Type": "text/plain; charset=utf-8"}, body=b"not found")

    def do_HEAD(self) -> None:  # noqa: N802
        # mirror GET headers
        path = (self.path or "/").split("?", 1)[0]
        base = self.base_path.rstrip("/") or "/timetable"
        if path.startswith(base.rstrip("/") + "/"):
            rel = path[len(base.rstrip("/") + "/") :]
            gid = rel
            if gid.endswith(".ics"):
                gid = gid[:-4]
            gid = gid.strip("/")
            file_path = _calendar_path(self.storage_root, gid)
            if os.path.isfile(file_path):
                try:
                    data = _read_file(file_path)
                except Exception:
                    self._send(
                        500, headers={"Content-Type": "text/plain; charset=utf-8"}, body=None
                    )
                    return
                etag = '"' + _md5_hex(data) + '"'
                inm = (self.headers.get("If-None-Match") or "").strip()
                if inm and inm == etag:
                    self._send(304, headers={"ETag": etag}, body=None)
                else:
                    headers = {"Content-Type": "text/calendar; charset=utf-8", "ETag": etag}
                    self._send(200, headers=headers, body=None)
                return
        self._send(404, headers={"Content-Type": "text/plain; charset=utf-8"}, body=None)


def start_timetable_server_background(
    *, host: str, port: int, base_path: str, storage_root: str, display_tz: str = "Europe/Moscow"
):
    """Start HTTP server in a daemon thread and return the thread."""
    _ensure_dir(storage_root)

    class Handler(_TimetableHandler):  # type: ignore
        pass

    Handler.base_path = base_path or "/timetable"
    Handler.storage_root = storage_root
    Handler.display_tz = display_tz or "Europe/Moscow"

    httpd = ThreadingHTTPServer((host, port), cast(type[BaseHTTPRequestHandler], Handler))

    def _run():
        logger.info(
            "Сервер расписания: http://{}:{}{}<group_id> (каталог: {}, часовой пояс: {})",
            host,
            port,
            base_path if base_path.startswith("/") else "/" + base_path,
            storage_root,
            Handler.display_tz,
        )
        try:
            httpd.serve_forever(poll_interval=0.5)
        except Exception as e:
            logger.exception("Сбой HTTP сервера расписания: {}", e)
        finally:
            try:
                httpd.server_close()
            except Exception:
                pass

    t = threading.Thread(target=_run, name="timetable-server", daemon=True)
    t.start()
    return t


def ensure_group_meta(storage_root: str, group_id: str, name: str) -> None:
    """Write per-group meta.json used by the landing page."""
    d = os.path.join(storage_root, group_id)
    _ensure_dir(d)
    meta = {"id": group_id, "name": name}
    tmp = os.path.join(d, "meta.json.tmp")
    path = os.path.join(d, "meta.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, path)


def write_groups_index(storage_root: str, group_names: list[str]) -> None:
    """Write groups.json with mapping of group_id to display name for landing.

    Derives ids from names using utils.group_id_from_name.
    """
    _ensure_dir(storage_root)
    mapping = []
    for g in group_names:
        gid = group_id_from_name(g)
        mapping.append({"id": gid, "name": g})
    tmp = os.path.join(storage_root, "groups.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"groups": mapping}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(storage_root, "groups.json"))
