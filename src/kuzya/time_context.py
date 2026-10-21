"""Wall-clock context for agent-facing prompts.

Every platform timestamp is naive UTC (``storage.models.utcnow``), while
users live in a local timezone. Without an explicit wall-clock line the
model reads raw UTC values as local time — a three-hour skew for a UTC-03
user ("the task finished at 15:00" when it is 13:00 locally).
:func:`time_context_line` is the single injection point stating the current
local time and the conversion rule; prompts embed it verbatim.
"""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

#: Env override for the user-facing timezone (an IANA name). Defaults to
#: the system zone; an invalid value falls back to it rather than killing
#: every agent turn.
TIMEZONE_ENV = "KUZYA_TIMEZONE"


def local_timezone() -> dt.tzinfo:
    """Return the user-facing timezone: ``KUZYA_TIMEZONE`` or system local."""
    raw = os.environ.get(TIMEZONE_ENV, "").strip()
    if raw:
        try:
            return ZoneInfo(raw)
        except Exception:
            pass
    return dt.datetime.now().astimezone().tzinfo


def time_context_line() -> str:
    """One line of wall-clock context plus the naive-UTC conversion rule."""
    tz = local_timezone()
    now = dt.datetime.now(tz)
    offset = now.utcoffset() or dt.timedelta(0)
    sign = "-" if offset < dt.timedelta(0) else "+"
    total_minutes = int(abs(offset).total_seconds() // 60)
    offset_text = f"UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"
    name = getattr(tz, "key", None) or now.tzname() or "system local"
    return (
        f"Текущее время: {now.strftime('%Y-%m-%d %H:%M')} {offset_text} "
        f"({name}). Все метки времени в API и базе — naive UTC: "
        "при ответе пользователю переводи их в это местное время."
    )
