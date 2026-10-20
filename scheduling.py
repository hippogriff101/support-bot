"""Timed helper messages: the `?@user` DM flow and Home-tab broadcasts.

Both features come down to the same two problems, so they share the code here:

  1. When? A helper picks a delay (now, in an hour, in 3 days…) in a modal and
     we store the resulting absolute UTC timestamp, so nothing depends on the
     process staying up in between.
  2. What does it say? Bodies are templates, not strings — see `render()`.

Rendering is deliberately tiny and hand-rolled rather than `str.format()` or
`string.Template`: helper text is full of Slack syntax that must survive
untouched, and a stray `{` shouldn't blow up a DM to a few hundred people.
Unknown placeholders are left as written so a helper can see (and fix) a typo
instead of silently sending a broken message.

Sending is split from rendering so a failed send to one person doesn't stop
the rest: the callers mark each recipient sent/failed individually.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

# Slack's maximum delay for chat.scheduleMessage. Bigger delays would have to
# be re-scheduled repeatedly, so they're refused up front with a clear error.
MAX_DELAY_SECONDS = 48 * 60 * 60

# Placeholders a helper can put in a message body. Keys are what they type.
PLACEHOLDERS = {
    "user-at": "a mention of the person (`<@U123>`) — pings them",
    "user": "the person's @name, without pinging",
    "name": "the person's first name",
    "date": "today's date, e.g. 2026-10-20",
    "event": "the event name from Settings",
    "helper": "the helper who wrote it, mentioned",
}

# `{name}` / `{user-at}`. Anything else (including `{{`) is left alone.
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9-]*)\}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def to_sqlite(moment: datetime) -> str:
    """A datetime as SQLite's CURRENT_TIMESTAMP format (UTC, second
    precision) so string comparisons against datetime('now') work."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def stagger(send_at: str, seconds: float) -> str:
    """Push a SQLite timestamp later by `seconds`, for spacing out a broadcast
    so each recipient's DM lands a rate-limit gap after the last."""
    try:
        base = datetime.strptime(send_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return send_at
    return to_sqlite(base + timedelta(seconds=seconds))


def parse_when(text: str, now: datetime | None = None) -> tuple[str | None, str | None]:
    """Turn a delay or timestamp into `(send_at, error)`.

    Accepts either a delay ("now", "3d", "2 days", "90 minutes", "1d 6h", "1.5")
    or an absolute "YYYY-MM-DD HH:MM" (UTC). Returns the SQLite timestamp and
    no error, or `(None, why)` — a message aimed at a human, not a stack trace.
    """
    now = now or _now()
    text = (text or "").strip()
    if not text or text.lower() in ("now", "0", "immediately"):
        return to_sqlite(now), None

    parsed = _parse_absolute(text)
    if parsed is not None:
        if parsed < now - timedelta(minutes=1):
            return None, "That time is in the past. Give me a future date and time (UTC)."
        if parsed - now > timedelta(seconds=MAX_DELAY_SECONDS):
            return None, _too_far()
        return to_sqlite(parsed), None

    seconds = _parse_delay(text)
    if seconds is None:
        return None, (
            "I couldn't read that. Use a delay like `now`, `2d`, `12h`, `90 minutes`, "
            "`1d 6h`, or a date like `2026-10-22 09:00` (UTC)."
        )
    if seconds > MAX_DELAY_SECONDS:
        return None, _too_far()
    return to_sqlite(now + timedelta(seconds=seconds)), None


def _too_far() -> str:
    return "That's more than 48 hours away, which is further than I can schedule. Try something sooner."


def _parse_absolute(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}

# "1d 6h", "90 minutes", "1.5h" — a number followed by a unit, repeated.
_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")


def _parse_delay(text: str) -> float | None:
    lowered = text.lower()
    if lowered.replace(".", "", 1).isdigit():  # a bare number means hours
        return float(lowered) * 3600
    matches = _PART_RE.findall(lowered)
    if not matches:
        return None
    seconds = 0.0
    for amount, unit in matches:
        if unit not in _UNITS:
            return None  # an unrecognised word means we'd be guessing
        seconds += float(amount) * _UNITS[unit]
    return seconds


def humanize(send_at: str, now: datetime | None = None) -> str:
    """'in 2 days 3h', 'in 40m', 'now' — for confirmations and list views."""
    try:
        when = datetime.strptime(send_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "?"
    seconds = (when - (now or _now())).total_seconds()
    if seconds <= 1:
        return "now"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def render(body: str, *, user_id: str = "", name: str = "", helper_id: str = "") -> str:
    """Fill `{name}`, `{user-at}`, `{user}`, `{date}`, `{event}`, `{helper}`.

    Unknown placeholders are left as typed. Multi-paragraph bodies, emoji,
    `*bold*`, links and code blocks all pass through untouched.
    """
    import config  # imported late: config reads the DB, and tests reload it

    values = {
        "user-at": f"<@{user_id}>" if user_id else "",
        "user": f"{name} (@{user_id})" if user_id else "",
        "name": name or "there",
        "date": _now().strftime("%Y-%m-%d"),
        "event": config.event_name(),
        "helper": f"<@{helper_id}>" if helper_id else "",
    }

    def replace(match: re.Match) -> str:
        if match.group(1) not in values:
            logging.debug("Unknown message placeholder %s — left as written", match.group(0))
            return match.group(0)
        return values[match.group(1)]

    return _PLACEHOLDER_RE.sub(replace, body)
