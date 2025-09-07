"""Small utilities shared across modules."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from loguru import logger


def stable_event_key(item: Mapping[str, Any]) -> str:
    """Return a stable string key for an event item.

    The key is used to detect additions/removals/changes and to build
    deterministic UIDs. It concatenates date, start, end, title, room, teacher.
    """

    return (
        f"{str(item.get('date',''))}|{str(item.get('start',''))}|{str(item.get('end',''))}|"
        f"{str(item.get('title',''))}|{str(item.get('room',''))}|{str(item.get('teacher',''))}"
    )


def group_id_from_name(name: str) -> str:
    """Extract a short group identifier from a display name.

    Rules:
    - Prefer leading digits (e.g., "104б__Философия" -> "104").
    - Fallback to lowercase alnum+underscore slug (digits and ASCII letters).
    - If nothing usable remains, return "grp".
    """

    import re as _re

    s = (name or "").strip()
    m = _re.match(r"^(\d+)", s)
    if m:
        return m.group(1)
    # Remove any non-alphanumeric characters (drop underscores too for cleaner IDs)
    slug = _re.sub(r"[^0-9A-Za-z]+", "", s).lower()
    return slug or "grp"


def logged_sleep(
    total_seconds: float,
    *,
    message: str = "Ожидание",
    tick_seconds: float = 1.0,
    bar_width: int = 30,
) -> None:
    """Sleep with a simple textual progress bar logged via loguru.

    - Logs a start message with total seconds.
    - Updates a single-line progress bar every `tick_seconds` using a carriage return.
    - Finishes with a newline and a debug message.
    """

    try:
        total = float(total_seconds)
    except Exception:
        total = 0.0
    if total <= 0:
        return

    # Announce wait (reduced verbosity)
    logger.debug("{}: {} сек.", message, int(total))

    start = time.monotonic()
    end = start + total

    # Draw progress bar updates
    while True:
        now = time.monotonic()
        remaining = max(0.0, end - now)
        elapsed = total - remaining
        frac = 0.0 if total <= 0 else min(1.0, elapsed / total)
        filled = int(round(bar_width * frac)) if bar_width > 0 else 0
        empty = bar_width - filled
        bar = (
            "[" + ("█" * filled) + (" " * max(0, empty)) + f"] {int(elapsed):02d}/{int(total):02d}s"
        )
        # Raw log line with carriage return to update in place (debug-level)
        logger.opt(raw=True).debug("\r" + bar)
        if remaining <= 0:
            break
        sleep_dur = tick_seconds if remaining > tick_seconds else remaining
        time.sleep(sleep_dur)

    # Move to next line after finishing the bar
    logger.opt(raw=True).debug("\n")
    logger.debug("Ожидание завершено")


def _log_samples(items: list[Mapping[str, Any]], *, max_items: int = 5) -> None:
    """Log a handful of parsed items for quick visibility in debug logs."""

    for sample in items[:max_items]:
        logger.debug(
            "Пример: {} {}-{} | {} | {} | {}",
            sample.get("date"),
            sample.get("start"),
            sample.get("end"),
            sample.get("title"),
            sample.get("room"),
            sample.get("teacher"),
        )
