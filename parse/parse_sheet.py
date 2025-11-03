from __future__ import annotations

import html
import re
from datetime import datetime
from typing import NotRequired, TypedDict, cast
from selenium.webdriver.remote.webdriver import WebDriver
from utils.config import AppConfig
from .browser import open_timetable_page
from .fill_columns import fill_filters

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Patterns
TIME_RANGE_IN_CONTENT_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})")
LANG_TITLE_RE = re.compile(r"^\s*([А-ЯЁ][а-яё]+)\s+язык\b")
ROOM_PREFIX_RE = re.compile(r"^\s*ауд\.?\s*", re.IGNORECASE)
ADDED_PREFIX_RE = re.compile(r"^\s*добавлено[:\s]*", re.IGNORECASE)


class ParsedItem(TypedDict, total=False):
    """Shape of a parsed timetable item."""

    date: str
    start: str
    end: str
    title: str
    room: NotRequired[str]
    teacher: NotRequired[str]
    group_info: NotRequired[str]
    added_at: NotRequired[str]
    pair: NotRequired[int | None]
    pair_label: NotRequired[str | None]
    raw: NotRequired[str]


def _is_probable_teacher_line(text: str, *, title_line: str) -> bool:
    """Heuristic to detect teacher lines and avoid subject titles.

    Rules:
    - Must not contain '[' (subject type markers like [Лк], [Сем]).
    - Prefer lines with initials (e.g., "Иванов И.") or 3-token FIO.
    - Allow comma-separated multiple teachers.
    - Avoid 2-word generic titles by requiring either initials, comma, or
      at least one token ending with common patronymic suffixes ("ич", "вна").
    """

    t = (text or "").strip()
    if not t or "[" in t:
        return False
    # If exactly equals to the title line, it's not a teacher line
    if t == (title_line or "").strip():
        return False

    # Comma-separated multiple teachers
    if "," in t:
        parts = [p.strip() for p in t.split(",") if p.strip()]
        if all(_is_probable_teacher_line(p, title_line=title_line) for p in parts):
            return True
        # Fallthrough to other checks

    # Initials pattern like "Иванов И." or "Иванов И.И."
    if re.search(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ](?:\.[А-ЯЁ]\.)?\.\b", t):
        return True

    tokens = t.split()
    # Three-word FIO
    if len(tokens) == 3 and all(re.match(r"^[А-ЯЁ][а-яё-]+$", x) for x in tokens):
        return True

    # Two-word but with a likely patronymic/surname suffix
    if (
        len(tokens) == 2
        and all(re.match(r"^[А-ЯЁ][а-яё-]+$", x) for x in tokens)
        and any(tokens[-1].endswith(suf) for suf in ("ич", "вна"))
    ):
        return True

    return False


def _split_html_lines(content: str) -> list[str]:
    """Split HTML content by <br> variants, unescape, strip empties."""

    if not content:
        return []
    # Normalize and split by any <br>, <br/>, <br /> (case-insensitive)
    text = html.unescape(content)
    parts = re.split(r"<\s*br\s*/?\s*>", text, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p and p.strip()]


def _extract_times(
    parts: list[str], default_start: str, default_end: str
) -> tuple[str, str, list[str]]:
    """Return (start, end, remaining_parts), removing initial explicit time range if present."""

    if parts and (m := TIME_RANGE_IN_CONTENT_RE.search(parts[0])):
        return m.group(1), m.group(2), parts[1:]
    return default_start, default_end, parts


def _extract_meta(parts: list[str], *, title_line: str) -> tuple[str, str, list[str], str]:
    """Extract (room, teacher, group_info_lines, added_at)."""

    room = ""
    teacher = ""
    added_at = ""
    groups: list[str] = []
    for p in parts:
        # Room: "ауд. ..." or "ауд ..."
        if ROOM_PREFIX_RE.match(p):
            room = ROOM_PREFIX_RE.sub("", p).strip()
            continue
        # Added at line, tolerate missing colon or spaces
        if ADDED_PREFIX_RE.match(p):
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", p)
            added_at = m.group(1) if m else ADDED_PREFIX_RE.sub("", p).strip()
            continue
        # Teacher heuristics (avoid using the title itself)
        if (not teacher) and _is_probable_teacher_line(p, title_line=title_line):
            teacher = p.strip()
            continue
        groups.append(p.strip())
    return room, teacher, groups, added_at


def _iso_date_from(date_str: str | None, *, raw_title: str | None = None) -> str | None:
    """Parse a header date into ISO (YYYY-MM-DD), tolerating missing year.

    Tries the following in order:
    - Exact dd.mm.YYYY from the header cell
    - dd.mm (no year) combined with the year extracted from raw_title (e.g., "10.11.2025 1 пара")
    Returns None if parsing fails.
    """
    s = (date_str or "").strip()
    if not s:
        return None
    # Try strict dd.mm.YYYY first
    try:
        dt = datetime.strptime(s, "%d.%m.%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    # Try dd.mm (yearless) with fallback year from raw_title
    try:
        dt = datetime.strptime(s, "%d.%m")
        year = None
        if raw_title:
            import re as _re

            m = _re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw_title)
            if m:
                year = int(m.group(3))
        if year is None:
            return None
        dt = dt.replace(year=year)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_table(driver, *, timeout: int = 20) -> list[ParsedItem]:
    """Parse the visible timetable table into a list of lesson dicts."""

    out: list[ParsedItem] = []
    table = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "table#timeTable"))
    )
    rows = table.find_elements(By.CSS_SELECTOR, "tbody > tr")

    current_dates: list[str] = []  # five dates for the current week row
    i = 0
    while i < len(rows):
        row = rows[i]

        # Day-of-week header row: collect five dates
        if row.find_elements(By.CSS_SELECTOR, "th.headday"):
            date_ths = row.find_elements(By.CSS_SELECTOR, "th.headdate > div")
            current_dates = [d.text.strip() for d in date_ths]  # DD.MM.YYYY
            i += 1
            continue

        # Regular lesson row: first th.headcol → default start/end time
        headcol = row.find_elements(By.CSS_SELECTOR, "th.headcol")
        if headcol:
            start_txt = headcol[0].find_element(By.CSS_SELECTOR, ".start").text.strip()
            end_txt = headcol[0].find_element(By.CSS_SELECTOR, ".end").text.strip()

            # Data cells for week columns: take last five tds to align properly
            tds = row.find_elements(By.CSS_SELECTOR, "td")
            week_cells = tds[-5:] if len(tds) >= 5 else tds

            for col_idx, td in enumerate(week_cells):
                cell = td.find_elements(By.CSS_SELECTOR, "div.cell")
                if not cell:
                    continue
                lesson_blocks = cell[0].find_elements(
                    By.CSS_SELECTOR, "div[class^='lesson-'] > div[data-content]"
                )
                # Fallback: some layouts may place data-content directly on the lesson node
                if not lesson_blocks:
                    lesson_blocks = cell[0].find_elements(By.CSS_SELECTOR, "div[data-content]")
                if not lesson_blocks:
                    continue

                date_str = current_dates[col_idx] if col_idx < len(current_dates) else None
                if not date_str:
                    continue

                for lb in lesson_blocks:
                    raw_content = lb.get_attribute("data-content") or ""
                    raw_title = lb.get_attribute("data-original-title") or ""

                    # Derive ISO date (fallback to popover title year if header misses it)
                    iso_date = _iso_date_from(date_str, raw_title=raw_title)
                    if not iso_date:
                        # Skip malformed dates rather than crashing the whole parse
                        continue

                    parts = _split_html_lines(raw_content)

                    # Optional custom time range in content
                    ev_start, ev_end, rem = _extract_times(parts, start_txt, end_txt)

                    # Title is the first remaining line after stripping an explicit time range
                    title_line = rem[0] if rem else (parts[0] if parts else "")
                    # Normalize to have a space before '[' kind markers
                    title_line_spaced = re.sub(r"\[", " [", title_line)

                    # Collect room, teacher, group_info, added_at
                    room, teacher, group_info_lines, added_at = _extract_meta(
                        rem, title_line=title_line
                    )

                    # Pair number from popover title (e.g., "08.09.2025 1 пара")
                    pair_no: int | None = None
                    pair_label: str | None = None
                    m_pair = re.search(r"\b(\d{1,2})\s*пара\b", raw_title)
                    if m_pair:
                        try:
                            pair_no = int(m_pair.group(1))
                        except Exception:
                            pair_no = None
                        pair_label = m_pair.group(0)

                    out.append(
                        cast(
                            ParsedItem,
                            {
                                "date": iso_date,
                                "start": ev_start,
                                "end": ev_end,
                                "title": title_line_spaced,
                                "room": room,
                                "teacher": teacher,
                                "group_info": "; ".join([g for g in group_info_lines if g]),
                                "added_at": added_at,
                                "pair": pair_no,
                                "pair_label": pair_label,
                                "raw": f"{raw_title} :: {html.unescape(raw_content)}",
                            },
                        )
                    )
        i += 1

    return out


def _extract_language_base(title: str) -> str | None:
    """Normalize a title and extract the base language name if present."""

    no_type = re.sub(r"\s*\[.*?\]\s*", " ", title or "").strip()
    m = LANG_TITLE_RE.match(no_type)
    if m:
        return m.group(1)
    return None


def group_language_lessons(items: list[ParsedItem]) -> list[ParsedItem]:
    """Group multiple foreign language lessons in the same timeslot into one event."""

    from collections import defaultdict

    groups = {}
    rest = []
    for it in items:
        base = _extract_language_base(it.get("title", ""))
        if not base:
            rest.append(it)
            continue
        key = (it["date"], it["start"], it["end"])
        if key not in groups:
            groups[key] = {"langs": defaultdict(set), "orig": []}
        if it.get("room"):
            groups[key]["langs"][base].add(it["room"].strip())
        else:
            _ = groups[key]["langs"][base]
        groups[key]["orig"].append(it)

    out: list[ParsedItem] = rest[:]
    for (date, start, end), data in groups.items():
        parts: list[str] = []
        all_rooms = set()
        pair_no = None
        pair_label = None
        added_at = None

        # Stable ordering: sort languages and rooms
        def _norm_room(r: str) -> str:
            return (r or "").strip().lower()

        for lang in sorted(list(data["langs"].keys())):
            rooms_set = data["langs"][lang]
            rooms_sorted = sorted([r for r in rooms_set], key=_norm_room)
            for r in rooms_sorted:
                all_rooms.add(r)
            if rooms_sorted:
                parts.append(f"{lang} " + ", ".join(rooms_sorted))
            else:
                parts.append(f"{lang}")

        for o in data["orig"]:
            if pair_no is None and o.get("pair"):
                pair_no = o.get("pair")
                pair_label = o.get("pair_label")
            if o.get("added_at") and not added_at:
                added_at = o.get("added_at")

        title = "; ".join(parts) if parts else "Иностранные языки"
        raw_sources = "; ".join(
            f"{o['title']} ({o.get('room','')})".strip()
            for o in sorted(data["orig"], key=lambda x: (x.get("title", ""), x.get("room", "")))
        )
        out.append(
            cast(
                ParsedItem,
                {
                    "date": date,
                    "start": start,
                    "end": end,
                    "title": title,
                    "room": ", ".join(sorted(all_rooms)) if all_rooms else "",
                    "teacher": "",
                    "group_info": "",
                    "pair": pair_no,
                    "pair_label": pair_label,
                    "added_at": added_at,
                    "raw": f"Сгруппировано из языковых занятий: {raw_sources}",
                },
            )
        )

    return out


def build_events_for_group(driver: WebDriver, cfg: AppConfig) -> list[ParsedItem]:
    """Apply filters for the given config and return parsed lesson items.

    Handles filter selection, table parsing, and optional language grouping.
    """

    # Ensure timetable page is loaded
    open_timetable_page(driver, cfg.base_url, timeout=cfg.selenium_timeout, retries=2)

    # Fill filters and wait for table
    fill_filters(
        driver,
        cfg.faculty,
        str(cfg.course),
        cfg.group,
        timeout=cfg.selenium_timeout,
    )

    items = parse_table(driver, timeout=cfg.selenium_timeout)
    if cfg.group_languages:
        before = len(items)
        items = group_language_lessons(items)
        if before != len(items):
            from loguru import logger as _logger

            _logger.info("Сгруппированы языки: {} → {}", before, len(items))
    return items
