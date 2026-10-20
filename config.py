"""Runtime-editable bot settings.

Each setting resolves in order: a value saved from the admin dashboard (the
`settings` table in tickets.db) → the matching env var → a built-in default.
So `.env` still works for a fixed deploy, but admins can change anything here
live from the bot's Home tab without a redeploy.
"""

import os

import db

# key -> (env var, default)
SETTINGS = {
    "support_channel_id": ("SUPPORT_CHANNEL_ID", ""),
    "event_name": ("EVENT_NAME", "Event"),
    # Days a user's message can sit without a helper reply before the nudge
    # persona pings the support channel about it. 0 turns nudges off.
    "stale_days": ("STALE_DAYS", "2"),
    "nudge_name": ("NUDGE_NAME", "Chopsticks"),
    # An emoji shortcode (`:chopsticks:`) or an image URL.
    "nudge_icon": ("NUDGE_ICON", ":chopsticks:"),
    # Seconds to wait between two broadcast DMs, so a broadcast to a few
    # hundred people doesn't trip Slack's per-channel rate limit. 1.2s ≈
    # 50/minute, which is what chat.postMessage allows.
    "broadcast_gap_seconds": ("BROADCAST_GAP_SECONDS", "1.2"),
}


def get(key: str, path: str | None = None) -> str:
    env_var, default = SETTINGS[key]
    saved = db.get_setting(f"config:{key}", path=path or db.DB_PATH)
    if saved is not None:
        return saved
    return os.environ.get(env_var) or default


def save(key: str, value: str, path: str | None = None) -> None:
    if key not in SETTINGS:
        raise KeyError(key)
    db.set_setting(f"config:{key}", value, path=path or db.DB_PATH)


def support_channel_id() -> str:
    return get("support_channel_id")


def event_name() -> str:
    return get("event_name")


def default_role() -> str:
    return f"{event_name()} Support"


def stale_days() -> float:
    try:
        return max(0.0, float(get("stale_days")))
    except ValueError:
        return 0.0


def nudge_identity() -> dict:
    """chat_postMessage kwargs that post as the nudge persona."""
    icon = get("nudge_icon")
    identity = {"username": get("nudge_name")}
    if icon.startswith("http"):
        identity["icon_url"] = icon
    elif icon:
        identity["icon_emoji"] = icon if icon.startswith(":") else f":{icon.strip(':')}:"
    return identity


def broadcast_gap_seconds() -> float:
    """Seconds between broadcast DMs. Clamped to Slack's ~50/min ceiling, and
    never negative."""
    try:
        return min(1.2, max(0.0, float(get("broadcast_gap_seconds"))))
    except ValueError:
        return 1.2
