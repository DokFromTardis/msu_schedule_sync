from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from tempfile import TemporaryDirectory

from sync.serve.timetable_server import _collect_landing_rows


def test_collect_landing_rows_converts_to_moscow_time():
    with TemporaryDirectory() as d:
        gid = "104"
        gdir = os.path.join(d, gid)
        os.makedirs(gdir, exist_ok=True)
        # Touch calendar.ics so the server considers the group present
        with open(os.path.join(gdir, "calendar.ics"), "wb") as f:
            f.write(b"BEGIN:VCALENDAR\nEND:VCALENDAR\n")
        # Write snapshot with generated_at in UTC
        dt_utc = datetime(2025, 9, 5, 10, 0, 0, tzinfo=UTC)
        snap = {"items": [], "generated_at": dt_utc.isoformat().replace("+00:00", "Z")}
        with open(os.path.join(gdir, "last_schedule.json"), "w", encoding="utf-8") as f:
            json.dump(snap, f)

        rows = _collect_landing_rows(d, display_tz="Europe/Moscow")
        assert rows, "rows should not be empty"
        gid_, count, updated = rows[0]
        assert gid_ == gid
        # 10:00 UTC = 13:00 MSK
        assert "13:00" in updated and ("МСК" in updated or "Europe/Moscow" in updated)
