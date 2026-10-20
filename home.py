"""Block Kit builders for the App Home tab and the admin modals it opens.

Pure functions of the current DB/roster/config state — no Slack client calls
— so app.py's handlers just publish/open whatever these return.

The Home tab has four faces:
  - first-run setup (nobody's on the roster yet): a button to claim admin
    with the one-time setup code from the bot's logs
  - everyone else who isn't a helper: a "DM me for help" notice
  - helpers: ticket stats, the unanswered queue, and the reply leaderboard
  - admins: all of that plus org-wide numbers, settings, and roster/macro
    management
"""

import json
from datetime import datetime, timezone

import config
import db
import helpers
import macros
import scheduling

BAR_LEN = 10
# Home views cap out at 100 blocks; keep each list well inside that.
MAX_UNANSWERED = 10
MAX_ROWS = 30


def stats_text(open_count: int, closed_count: int) -> str:
    total = open_count + closed_count
    if total:
        filled = round(BAR_LEN * closed_count / total)
    else:
        filled = 0
    bar = "█" * filled + "░" * (BAR_LEN - filled)
    ratio_line = f"`[{bar}]` {closed_count}/{total} resolved" if total else f"`[{bar}]` No tickets yet"
    return f":bar_chart: *Support stats*\n• Open: *{open_count}*\n• Resolved: *{closed_count}*\n{ratio_line}"


def ago(sqlite_ts: str | None) -> str:
    """'3d 4h' / '5h 12m' / '12m' since a SQLite CURRENT_TIMESTAMP (UTC)."""
    if not sqlite_ts:
        return "?"
    then = datetime.strptime(sqlite_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    minutes = max(0, int((datetime.now(timezone.utc) - then).total_seconds() // 60))
    days, minutes = divmod(minutes, 60 * 24)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def permalink(channel: str, ts: str) -> str:
    return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _snippet(text: str | None, length: int = 60) -> str:
    flat = " ".join((text or "").split()) or "(attachment)"
    flat = flat if len(flat) <= length else flat[: length - 1] + "…"
    return _escape(flat)


def unanswered_lines(limit: int = MAX_UNANSWERED) -> list[str]:
    """One line per unanswered ticket, longest-waiting first, linking to the
    ticket's thread in the support channel."""
    channel = config.support_channel_id()
    lines = []
    for t in db.list_unanswered(limit=limit):
        waiting = ago(t["last_user_at"] or t["created_at"])
        line = f"• <{permalink(channel, t['support_ts'])}|{_snippet(t['original_text'])}> from <@{t['user_id']}>"
        line += f" — waiting *{waiting}*"
        if t["claimed_by"]:
            line += f" · claimed by <@{t['claimed_by']}>"
        lines.append(line)
    return lines


def _section(text: str, accessory: dict | None = None) -> dict:
    block = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if accessory:
        block["accessory"] = accessory
    return block


def _button(text: str, action_id: str, value: str = "", style: str | None = None) -> dict:
    button = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id}
    if value:
        button["value"] = value
    if style:
        button["style"] = style
    return button


def _overflow(action_id: str, options: list[tuple[str, str]]) -> dict:
    return {
        "type": "overflow",
        "action_id": action_id,
        "options": [{"text": {"type": "plain_text", "text": label}, "value": value} for label, value in options],
    }


def build_home_view(user_id: str) -> dict:
    if not helpers.all_helpers():
        return {"type": "home", "blocks": _setup_blocks()}
    if not helpers.is_helper(user_id):
        return {
            "type": "home",
            "blocks": [
                _section(
                    f":wave: *{config.event_name()} Support*\nDM me here if you need help — "
                    "I'll check whether you want it sent to the support team."
                )
            ],
        }

    is_admin = helpers.is_admin(user_id)
    blocks = _helper_blocks(is_admin, user_id)
    blocks += _scheduled_blocks(user_id)
    if is_admin:
        blocks += _admin_blocks()
    return {"type": "home", "blocks": blocks}


def _setup_blocks() -> list:
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Welcome! Let's set up the support bot"}},
        _section(
            "Nobody's been made an admin yet. Whoever's deploying the bot can claim admin with the "
            "one-time *setup code* printed in the bot's logs on startup, then configure everything "
            "(support channel, event name, nudges, helpers, macros) right here."
        ),
        {"type": "actions", "elements": [_button("Set up the bot", "open_setup", style="primary")]},
    ]


def _helper_blocks(is_admin: bool, user_id: str = "") -> list:
    open_count = db.count_tickets_by_status("open")
    closed_count = db.count_tickets_by_status("closed")

    toolbar = [_button(":arrows_counterclockwise: Refresh", "home_refresh")]
    if is_admin:
        toolbar += [
            _button(":gear: Settings", "open_config"),
            _button(":bust_in_silhouette: Add helper", "open_add_helper"),
            _button(":memo: Add macro", "open_add_macro"),
        ]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{config.event_name()} Support"}},
        {"type": "actions", "elements": toolbar},
    ]
    if not config.support_channel_id():
        blocks.append(
            _section(
                ":warning: *No support channel is configured* — DMs to the bot won't go anywhere yet. "
                + ("Hit *Settings* to pick one." if is_admin else "Ask an admin to set one up.")
            )
        )
    blocks.append(_section(stats_text(open_count, closed_count)))

    blocks.append({"type": "divider"})
    total_unanswered = db.count_unanswered()
    lines = unanswered_lines()
    if lines:
        heading = f":hourglass_flowing_sand: *Waiting on a reply ({total_unanswered})*"
        if total_unanswered > len(lines):
            heading += f" — oldest {len(lines)} shown"
        blocks.append(_section(heading + "\n" + "\n".join(lines)))
    else:
        blocks.append(_section(":white_check_mark: *Waiting on a reply*\nNothing — every open ticket has a reply."))

    blocks.append({"type": "divider"})
    reply_counts = db.count_replies_by_helper()
    resolved_counts = db.count_resolved_by_helper() if is_admin else {}
    ordered = sorted(helpers.all_helpers().items(), key=lambda kv: reply_counts.get(kv[0], 0), reverse=True)
    rows = []
    for uid, info in ordered[:MAX_ROWS]:
        line = f"• <@{uid}> ({info.get('role', config.default_role())}) — *{reply_counts.get(uid, 0)}* replies"
        if is_admin:
            line += f", *{resolved_counts.get(uid, 0)}* resolved"
        rows.append(line)
    blocks.append(_section("*Support agents*\n" + "\n".join(rows)))
    return blocks


def _scheduled_blocks(user_id: str) -> list:
    """Queued `?@user` messages, so a helper can see what they've set up."""
    blocks = []

    mine = db.list_scheduled_dms(user_id)
    if mine:
        blocks.append({"type": "divider"})
        lines = [
            f"• {scheduling.humanize(row['send_at'])} — to <@{row['recipient_id']}>: {_snippet(row['body'], 60)}"
            for row in mine[:MAX_ROWS]
        ]
        blocks.append(_section(f":alarm_clock: *Your scheduled messages ({len(mine)})*\n" + "\n".join(lines)))
    return blocks


def _admin_blocks() -> list:
    blocks = [
        {"type": "divider"},
        _section(
            "*Admin stats*\n"
            f"• People who've DM'd the bot: *{db.count_unique_requesters()}*\n"
            f"• Tickets all-time: *{db.count_tickets_total()}*\n"
            f"• Tickets today: *{db.count_tickets_today()}*"
        ),
        {"type": "divider"},
    ]

    channel = config.support_channel_id()
    stale = config.stale_days()
    nudges = f"after *{stale:g}* day(s), as *{config.get('nudge_name')}*" if stale else "*off*"
    blocks.append(
        _section(
            ":gear: *Settings*\n"
            f"• Event name: *{_escape(config.event_name())}*\n"
            f"• Support channel: {f'<#{channel}>' if channel else '_not set_'}\n"
            f"• Unanswered-ticket nudges: {nudges}",
            accessory=_button("Edit", "open_config"),
        )
    )

    blocks.append({"type": "divider"})
    roster = sorted(helpers.all_helpers().items())
    blocks.append(
        _section(f":busts_in_silhouette: *Helpers ({len(roster)})*", accessory=_button("Add", "open_add_helper"))
    )
    for uid, info in roster[:MAX_ROWS]:
        badge = " · *admin*" if info.get("admin") else ""
        blocks.append(
            _section(
                f"<@{uid}> — {_escape(info.get('role', config.default_role()))}{badge}",
                accessory=_overflow("helper_menu", [("Edit", f"edit:{uid}"), ("Remove", f"remove:{uid}")]),
            )
        )
    if len(roster) > MAX_ROWS:
        blocks.append(_section(f"_…and {len(roster) - MAX_ROWS} more — see `/support list`._"))

    blocks.append({"type": "divider"})
    all_macros = sorted(macros.all_macros().items())
    blocks.append(_section(f":memo: *Macros ({len(all_macros)})*", accessory=_button("Add", "open_add_macro")))
    for name, text in all_macros[:MAX_ROWS]:
        blocks.append(
            _section(
                f"`?{name}` — {_snippet(text, 120)}",
                accessory=_overflow("macro_menu", [("Edit", f"edit:{name}"), ("Delete", f"delete:{name}")]),
            )
        )
    if len(all_macros) > MAX_ROWS:
        blocks.append(_section(f"_…and {len(all_macros) - MAX_ROWS} more — see `/support macros`._"))
    return blocks


# --- Modals ----------------------------------------------------------------


def _modal(callback_id: str, title: str, blocks: list, submit: str = "Save", metadata: dict | None = None) -> dict:
    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": json.dumps(metadata or {}),
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": submit},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _text_input(block_id: str, label: str, initial: str = "", multiline: bool = False, hint: str = "") -> dict:
    element = {"type": "plain_text_input", "action_id": "value", "multiline": multiline}
    if initial:
        element["initial_value"] = initial
    block = {"type": "input", "block_id": block_id, "label": {"type": "plain_text", "text": label}, "element": element}
    if hint:
        block["hint"] = {"type": "plain_text", "text": hint}
    return block


def setup_modal() -> dict:
    return _modal(
        "setup_submit",
        "Set up the bot",
        [
            _section("Paste the setup code from the bot's startup logs to become its first admin."),
            _text_input("code", "Setup code"),
            _text_input("role", "Your role", initial=config.default_role(), hint="Shown in your reply signature."),
        ],
        submit="Claim admin",
    )


def config_modal() -> dict:
    channel_select = {
        "type": "conversations_select",
        "action_id": "value",
        "filter": {"include": ["public", "private"], "exclude_bot_users": True},
    }
    if config.support_channel_id():
        channel_select["initial_conversation"] = config.support_channel_id()

    return _modal(
        "config_submit",
        "Bot settings",
        [
            _text_input("event_name", "Event name", initial=config.event_name()),
            {
                "type": "input",
                "block_id": "support_channel_id",
                "label": {"type": "plain_text", "text": "Support channel"},
                "hint": {"type": "plain_text", "text": "Invite the bot to it first (/invite)."},
                "element": channel_select,
            },
            {
                "type": "input",
                "block_id": "stale_days",
                "label": {"type": "plain_text", "text": "Nudge about unanswered tickets after (days)"},
                "hint": {"type": "plain_text", "text": "Decimals work (0.5 = 12 hours). 0 turns nudges off."},
                "element": {
                    "type": "number_input",
                    "action_id": "value",
                    "is_decimal_allowed": True,
                    "min_value": "0",
                    "initial_value": f"{config.stale_days():g}",
                },
            },
            _text_input("nudge_name", "Nudge name", initial=config.get("nudge_name")),
            _text_input(
                "nudge_icon",
                "Nudge icon",
                initial=config.get("nudge_icon"),
                hint="An emoji like :chopsticks: or a URL.",
            ),
        ],
    )


def helper_modal(user_id: str | None = None) -> dict:
    info = helpers.all_helpers().get(user_id, {}) if user_id else {}
    user_select = {"type": "users_select", "action_id": "value"}
    if user_id:
        user_select["initial_user"] = user_id
    admin_option = {"text": {"type": "plain_text", "text": "Admin (can manage settings, helpers and macros)"}}
    admin_option["value"] = "admin"
    checkboxes = [
        {"type": "checkboxes", "action_id": "admin", "options": [admin_option]},
    ]
    if info.get("admin"):
        checkboxes[0]["initial_options"] = [admin_option]

    return _modal(
        "helper_submit",
        "Edit helper" if user_id else "Add helper",
        [
            {
                "type": "input",
                "block_id": "user",
                "label": {"type": "plain_text", "text": "Person"},
                "element": user_select,
            },
            _text_input("role", "Role", initial=info.get("role", config.default_role())),
            {
                "type": "input",
                "block_id": "admin",
                "optional": True,
                "label": {"type": "plain_text", "text": "Permissions"},
                "element": checkboxes[0],
            },
        ],
        metadata={"editing": user_id},
    )


def macro_modal(name: str | None = None) -> dict:
    text = macros.get_macro(name) if name else ""
    return _modal(
        "macro_submit",
        "Edit macro" if name else "Add macro",
        [
            _text_input("name", "Name", initial=name or "", hint="Helpers send it with ?name in a ticket thread."),
            _text_input("text", "Reply text", initial=text or "", multiline=True),
        ],
        metadata={"editing": name},
    )


def confirm_modal(callback_id: str, title: str, text: str, submit: str, metadata: dict) -> dict:
    return _modal(callback_id, title, [_section(text)], submit=submit, metadata=metadata)


def _delay_input(block_id: str, label: str = "Send after", hint: str = "") -> dict:
    return _text_input(
        block_id,
        label,
        hint=hint
        or "A delay like `now`, `2d`, `12h`, `90 minutes`, `1d 6h`, or a date like `2026-10-22 09:00` (UTC). "
        "Up to 48 hours.",
    )


# --- Setting up a DM with somebody (?@user) ---------------------------------


def dm_setup_modal(target_id: str, context: dict | None = None) -> dict:
    """The popup behind `?@user`: who to message, what to say, and when.
    `context` (the Continue button's payload — where it was typed, the ticket
    it came from) rides along whole in the metadata for the submit handler."""
    return _modal(
        "dm_setup_submit",
        "Set up a DM",
        [
            _section(
                f"First message to send to <@{target_id}>. `{{name}}` becomes their first name and "
                "`{user-at}` mentions them."
            ),
            {
                "type": "input",
                "block_id": "target",
                "label": {"type": "plain_text", "text": "Send to"},
                "element": {"type": "users_select", "action_id": "value", "initial_user": target_id},
            },
            _text_input("message", "Message", multiline=True, hint="Slack formatting and emoji both work."),
            _delay_input("delay", "Send after"),
        ],
        submit="Send",
        metadata={**(context or {}), "target": target_id},
    )
