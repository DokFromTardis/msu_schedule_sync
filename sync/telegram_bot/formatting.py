"""Telegram HTML formatter for weekly schedule messages and diff helpers."""

from __future__ import annotations

import html as _html
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from utils import stable_event_key

# ---------- Helpers: escaping and RU locale ----------


def tg_escape(text: str) -> str:
    """Escape text for Telegram HTML parse_mode (escape &, <, >)."""
    return _html.escape(text or "", quote=False)


RU_MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]

RU_WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def format_ru_date(d: date) -> str:
    return f"{d.day} {RU_MONTHS[d.month - 1]}"


def weekday_short_ru(d: date) -> str:
    return RU_WEEKDAYS_SHORT[d.weekday()]


# ---------- Data normalization ----------


TIME_RANGE_ANY = re.compile(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})")


def _extract_title_and_kind(title: str) -> tuple[str, str | None]:
    """Split title like "История России [Сем]" into (title, kind)."""
    if not title:
        return "", None
    m = re.search(r"\[(.*?)\]", title)
    kind = m.group(1).strip() if m else None
    core = re.sub(r"\s*\[.*?\]\s*", " ", title).strip()
    return core, kind


def _normalize_title_for_compare(title: str, *, start: str, end: str) -> str:
    """Normalize title for comparison: strip kind, collapse spaces, and remove time if it matches slot."""
    core, _ = _extract_title_and_kind(title)
    disp = re.sub(r"\s+", " ", core).strip()

    # Remove embedded time if equals to slot times
    def _rm_time(m: re.Match[str]) -> str:
        t1, t2 = m.group(1), m.group(2)
        if t1 == start and t2 == end:
            return ""  # drop
        return m.group(0)

    disp = TIME_RANGE_ANY.sub(_rm_time, disp)
    return re.sub(r"\s+", " ", disp).strip()


def _title_for_display(title: str, *, start: str, end: str) -> tuple[str, str | None]:
    """Return (display_title_without_kind, kind) with repeated time removed."""
    core, kind = _extract_title_and_kind(title)

    # Remove embedded repeated time equal to slot
    def _rm_time(m: re.Match[str]) -> str:
        t1, t2 = m.group(1), m.group(2)
        if t1 == start and t2 == end:
            return ""
        return m.group(0)

    core = TIME_RANGE_ANY.sub(_rm_time, core)
    core = re.sub(r"\s+", " ", core).strip()
    return core, kind


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _time_key(t: str) -> tuple[int, int]:
    try:
        hh, mm = t.split(":")
        return int(hh), int(mm)
    except Exception:
        return (0, 0)


def dedupe_day_lessons(lessons: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Drop consecutive duplicates within a day by (start,end,normalized title,kind,room,teacher).

    Normalized title removes [kind] and any time matching the slot.
    """

    out: list[Mapping[str, Any]] = []
    prev_key: tuple[str, str, str, str | None, str, str] | None = None
    for it in lessons:
        start = it.get("start", "")
        end = it.get("end", "")
        title = it.get("title", "")
        room = it.get("room", "") or ""
        teacher = it.get("teacher", "") or ""
        norm_title = _normalize_title_for_compare(title, start=start, end=end)
        _, kind = _extract_title_and_kind(title)
        key = (start, end, norm_title.lower(), (kind or "").lower(), room.strip(), teacher.strip())
        if prev_key is not None and key == prev_key:
            # duplicate, skip
            continue
        out.append(it)
        prev_key = key
    return out


# ---------- Language block helpers ----------


LANG_FLAGS = {
    "Английский": "🇬🇧",
    "Немецкий": "🇩🇪",
    "Французский": "🇫🇷",
    "Испанский": "🇪🇸",
    "Итальянский": "🇮🇹",
    "Китайский": "🇨🇳",
    "Японский": "🇯🇵",
}


def _parse_languages_from_grouped_title(title: str) -> list[tuple[str, list[str]]]:
    """Best-effort parse of grouped language title 'Английский r1, r2; Немецкий r3'."""
    if not title:
        return []
    parts = [p.strip() for p in title.split(";") if p.strip()]
    result: list[tuple[str, list[str]]] = []
    for p in parts:
        tokens = p.split()
        if not tokens:
            continue
        lang = tokens[0]
        rooms_str = p[len(lang) :].strip()
        rooms = [s.strip() for s in rooms_str.split(",") if s.strip()]
        result.append((lang, rooms))
    return result


def _is_language_block(it: Mapping[str, Any]) -> bool:
    title = it.get("title", "")
    # heuristic: contains semicolon-separated languages or starts with one known language name
    langs = _parse_languages_from_grouped_title(title)
    return len(langs) >= 2


def build_language_bullets(title: str) -> list[str]:
    """Return bullet lines for a grouped language title.

    Example:
    - "🇬🇧 Английский: r1, r2"
    - "- 🇩🇪 Немецкий: r3"
    """
    lines: list[str] = []
    for lang, rooms in _parse_languages_from_grouped_title(title):
        flag = LANG_FLAGS.get(lang, "")
        lang_label = f"{flag} {lang}".strip()
        room_list = ", ".join(rooms)
        lines.append(f"- {lang_label}: {room_list}")
    return lines


# ---------- Public formatter ----------


def format_week_message(
    week_items: Iterable[Mapping[str, Any]], *, header_title: str, header_span: str
) -> str:
    """Build HTML-formatted weekly message from parsed timetable items.

    Steps: group by day, sort by start, dedupe consecutive duplicates, normalize titles, and format lines.
    """

    # Group by date
    by_day: dict[date, list[Mapping[str, Any]]] = {}
    for it in week_items:
        try:
            d = _parse_date(it.get("date", ""))
        except Exception:
            continue
        by_day.setdefault(d, []).append(it)

    # Header
    lines: list[str] = [f"📅 {tg_escape(header_title)} <i>{tg_escape(header_span)}</i>", ""]

    for day in sorted(by_day.keys()):
        items = by_day[day]
        # Sort by start time
        items.sort(key=lambda x: _time_key(x.get("start", "")))
        # Dedupe consecutive
        items = dedupe_day_lessons(items)

        # Day header
        lines.append(f"<b>📌 {format_ru_date(day)} ({weekday_short_ru(day)})</b>")
        # Lessons
        for idx, it in enumerate(items, start=1):
            start = it.get("start", "")
            end = it.get("end", "")
            title = it.get("title", "")
            room = it.get("room") or ""
            teacher = (it.get("teacher") or "").strip()

            disp_title, kind = _title_for_display(title, start=start, end=end)
            time_span = f"{start}–{end}" if start and end else ""

            # Language blocks: only language bullets, no base line
            if _is_language_block(it):
                # Header line with time and pair only
                parts: list[str] = ["- ⏰ "]
                pair_no = it.get("pair")
                pair_label = it.get("pair_label") or (f"{pair_no} пара" if pair_no else None)
                num_label = pair_label or f"{idx} пара"
                parts.append(f"<b>{tg_escape(num_label)}</b>")
                if time_span:
                    parts.append(f" <i>({tg_escape(time_span)})</i>")
                lines.append("".join(parts))
                # Then language bullets
                for lang, rooms in _parse_languages_from_grouped_title(title):
                    flag = LANG_FLAGS.get(lang, "")
                    lang_label = f"{flag} {lang}".strip()
                    room_list = ", ".join(rooms)
                    lines.append(f"- {lang_label}: {room_list}")
            else:
                # Base line
                parts: list[str] = ["- ⏰ "]
                # Prefer provided pair number/label over positional index
                pair_no = it.get("pair")
                pair_label = it.get("pair_label") or (f"{pair_no} пара" if pair_no else None)
                num_label = pair_label or f"{idx} пара"
                parts.append(f"<b>{tg_escape(num_label)}</b>")
                if time_span:
                    parts.append(f" <i>({tg_escape(time_span)})</i>")
                if disp_title:
                    parts.append(f" — <i>{tg_escape(disp_title)}</i>")
                if kind:
                    parts.append(f" [{tg_escape(kind)}]")
                if room:
                    parts.append(f" ({tg_escape(room)})")
                # Teacher block
                base_line = "".join(parts)
                if teacher:
                    base_line += f" — 🧑‍🏫 {tg_escape(teacher)}"
                lines.append(base_line)

        lines.append("")  # blank line after each day

    # Trim trailing blank line
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# ---------- Snapshot diff helpers ----------


def normalize_title_for_key(title: str) -> str:
    """Normalize title for change detection.

    - Drop bracketed [type]
    - If grouped language title, sort languages and rooms for stability
    - Collapse spaces and lowercase
    """
    import re as _re

    base = _re.sub(r"\s*\[.*?\]\s*", " ", title or "").strip()
    if ";" in base:
        parts = _parse_languages_from_grouped_title(base)
        if parts:

            def _norm_room(x: str) -> str:
                s = _re.sub(r"\s+", " ", (x or "").strip()).lower()
                s = _re.sub(r"\*+$", "", s)
                s = _re.sub(r"it\*?$", "", s)
                s = _re.sub(r"it$", "", s)
                return s

            def _norm(x: str) -> str:
                return _re.sub(r"\s+", " ", (x or "").strip()).lower()

            langs: list[tuple[str, list[str]]] = []
            for lang, rooms in parts:
                langs.append((lang, sorted([_norm_room(r) for r in rooms])))
            langs.sort(key=lambda x: _norm(x[0]))
            canon = "; ".join(
                [f"{lang} " + ", ".join(rs) if rs else f"{lang}" for (lang, rs) in langs]
            )
            return _re.sub(r"\s+", " ", canon).lower()
    return _re.sub(r"\s+", " ", base).lower()


def _stable_event_id(item: Mapping[str, Any]) -> str:
    import hashlib as _hashlib

    key = stable_event_key(item)
    return _hashlib.md5(key.encode("utf-8")).hexdigest()


def compute_schedule_diff(
    prev: Iterable[Mapping[str, Any]],
    curr: Iterable[Mapping[str, Any]],
) -> dict[str, list[tuple[dict[str, Any] | None, dict[str, Any] | None]]]:
    """Return dict with keys: added, removed, modified."""

    prev_map = {_stable_event_id(it): dict(it) for it in prev}
    curr_map = {_stable_event_id(it): dict(it) for it in curr}

    added: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    removed: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    modified: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []

    for ev_id, it in curr_map.items():
        if ev_id not in prev_map:
            added.append((None, it))
    for ev_id, it in prev_map.items():
        if ev_id not in curr_map:
            removed.append((it, None))

    def soft_key(it: Mapping[str, Any]) -> str:
        return "|".join(
            [
                str(it.get("date", "")),
                str(it.get("start", "")),
                str(it.get("end", "")),
                normalize_title_for_key(str(it.get("title", ""))),
            ]
        )

    removed_by_key: dict[str, list[dict[str, Any]]] = {}
    for old, _ in removed:
        if not old:
            continue
        removed_by_key.setdefault(soft_key(old), []).append(old)
    new_by_key: dict[str, list[dict[str, Any]]] = {}
    for _, new in added:
        if not new:
            continue
        new_by_key.setdefault(soft_key(new), []).append(new)

    paired_old = set()
    paired_new = set()
    for key, olds in removed_by_key.items():
        news = new_by_key.get(key, [])
        while olds and news:
            o = olds.pop(0)
            n = news.pop(0)
            modified.append((o, n))
            paired_old.add(id(o))
            paired_new.add(id(n))

    added = [(None, n) for (_, n) in added if id(n) not in paired_new]
    removed = [(o, None) for (o, _) in removed if id(o) not in paired_old]

    def _same_semantics(o: dict[str, Any] | None, n: dict[str, Any] | None) -> bool:
        if not o or not n:
            return False
        import re as _re

        t1 = normalize_title_for_key(str(o.get("title", "")))
        t2 = normalize_title_for_key(str(n.get("title", "")))

        def _canon_rooms(s: str) -> str:
            parts = [p.strip() for p in (s or "").split(",") if p.strip()]
            out: list[str] = []
            for p in parts:
                x = _re.sub(r"\s+", " ", p.strip()).lower()
                x = _re.sub(r"\*+$", "", x)
                x = _re.sub(r"it\*?$", "", x)
                x = _re.sub(r"it$", "", x)
                out.append(x)
            return ",".join(sorted(out))

        r1 = _canon_rooms(str((o.get("room") or "").strip()))
        r2 = _canon_rooms(str((n.get("room") or "").strip()))
        u1 = str((o.get("teacher") or "").strip())
        u2 = str((n.get("teacher") or "").strip())
        return (t1 == t2) and (r1 == r2) and (u1 == u2)

    modified = [(o, n) for (o, n) in modified if not _same_semantics(o, n)]
    return {"added": added, "removed": removed, "modified": modified}


def _fmt_item(it: Mapping[str, Any]) -> str:
    d = it.get("date", "")
    t = f"{it.get('start','')}–{it.get('end','')}"
    title = it.get("title", "")
    room = it.get("room") or ""
    teacher = it.get("teacher") or ""
    parts = [f"{d} {t}", title]
    if room:
        parts.append(f"({room})")
    if teacher:
        parts.append(f"— {teacher}")
    return " ".join(p for p in parts if p)


def format_diff(
    diff: dict[str, list[tuple[dict[str, Any] | None, dict[str, Any] | None]]], *, limit: int = 10
) -> str:
    a, r, m = diff["added"], diff["removed"], diff["modified"]
    lines: list[str] = []
    lines.append(f"Обновление расписания: +{len(a)}, −{len(r)}, ✏️ {len(m)}")

    def add_block(title: str, items: list[str]):
        if not items:
            return
        lines.append("")
        lines.append(title)
        lines.extend(items[:limit])
        if len(items) > limit:
            lines.append(f"… и еще {len(items) - limit}")

    add_block("Добавлено:", ["➕ " + _fmt_item(n) for _, n in a if n])
    add_block("Удалено:", ["➖ " + _fmt_item(o) for o, _ in r if o])
    mod_lines: list[str] = []
    for o, n in m:
        if not o or not n:
            continue
        left = _fmt_item(o)
        right = _fmt_item(n)
        mod_lines.append("✏️ {" + left + "} -> {" + right + "}")
    add_block("Изменено:", mod_lines)
    return "\n".join(lines)


# ---------- Period helpers ----------


def format_period(items: list[Mapping[str, Any]], *, title: str) -> str:
    if not items:
        return f"{title}\nНет занятий."
    by_date: dict[str, list[Mapping[str, Any]]] = {}
    for it in sorted(items, key=lambda x: (x.get("date", ""), x.get("start", ""))):
        by_date.setdefault(str(it.get("date", "")), []).append(it)
    lines: list[str] = [title]
    for d, lst in by_date.items():
        lines.append(f"\n{d}")
        for it in lst:
            t = f"{it.get('start','')}–{it.get('end','')}"
            room = f" ({it['room']})" if it.get("room") else ""
            teacher = f" — {it['teacher']}" if it.get("teacher") else ""
            lines.append(f"• {t} {it.get('title','')}{room}{teacher}")
    return "\n".join(lines)


def filter_items_by_date_range(
    items: Iterable[Mapping[str, Any]], *, start: date, end: date
) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for it in items:
        try:
            d = datetime.strptime(str(it.get("date", "")), "%Y-%m-%d").date()
        except Exception:
            continue
        if start <= d <= end:
            out.append(it)
    return out


def format_day_message(
    day_items: Iterable[Mapping[str, Any]], *, header_title: str, header_span: str
) -> str:
    """Build HTML-formatted day message using same item formatting as weekly."""

    # Group by day to reuse the same block formatting
    by_day: dict[date, list[Mapping[str, Any]]] = {}
    for it in day_items:
        try:
            d = _parse_date(it.get("date", ""))
        except Exception:
            continue
        by_day.setdefault(d, []).append(it)

    lines: list[str] = [f"📅 {tg_escape(header_title)} <i>{tg_escape(header_span)}</i>", ""]
    for day, items in by_day.items():
        # Sort and dedupe like in weekly
        items.sort(key=lambda x: _time_key(x.get("start", "")))
        items = dedupe_day_lessons(items)
        # Day header
        lines.append(f"<b>📌 {format_ru_date(day)} ({weekday_short_ru(day)})</b>")
        for idx, it in enumerate(items, start=1):
            start = it.get("start", "")
            end = it.get("end", "")
            title = it.get("title", "")
            room = it.get("room") or ""
            teacher = (it.get("teacher") or "").strip()
            disp_title, kind = _title_for_display(title, start=start, end=end)
            time_span = f"{start}–{end}" if start and end else ""

            if _is_language_block(it):
                parts: list[str] = ["- ⏰ "]
                pair_no = it.get("pair")
                pair_label = it.get("pair_label") or (f"{pair_no} пара" if pair_no else None)
                num_label = pair_label or f"{idx} пара"
                parts.append(f"<b>{tg_escape(num_label)}</b>")
                if time_span:
                    parts.append(f" <i>({tg_escape(time_span)})</i>")
                lines.append("".join(parts))
                for lang, rooms in _parse_languages_from_grouped_title(title):
                    flag = LANG_FLAGS.get(lang, "")
                    lang_label = f"{flag} {lang}".strip()
                    room_list = ", ".join(rooms)
                    lines.append(f"- {lang_label}: {room_list}")
            else:
                parts: list[str] = ["- ⏰ "]
                pair_no = it.get("pair")
                pair_label = it.get("pair_label") or (f"{pair_no} пара" if pair_no else None)
                num_label = pair_label or f"{idx} пара"
                parts.append(f"<b>{tg_escape(num_label)}</b>")
                if time_span:
                    parts.append(f" <i>({tg_escape(time_span)})</i>")
                if disp_title:
                    parts.append(f" — <i>{tg_escape(disp_title)}</i>")
                if kind:
                    parts.append(f" [{tg_escape(kind)}]")
                if room:
                    parts.append(f" ({tg_escape(room)})")
                base_line = "".join(parts)
                if teacher:
                    base_line += f" — 🧑‍🏫 {tg_escape(teacher)}"
                lines.append(base_line)

        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)
