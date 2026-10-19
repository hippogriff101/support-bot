import json
import logging
import os
import re
import secrets
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import attachments
import config
import db
import helpers
import home
import macros

load_dotenv()
logging.basicConfig(level=logging.INFO)

STATS_SETTING = "anchored_stats_ts"
RESOLVED_EMOJI = "ms-green-tick"
NUDGE_CHECK_SECONDS = 10 * 60
# Message subtypes that are still a person talking: plain messages, file
# uploads, and "also send to channel" thread replies.
RELAYED_SUBTYPES = {None, "file_share", "thread_broadcast"}
# `?word`s that are bot commands (or `/support macros` subcommands), so
# can't be macro names.
RESERVED_MACRO_NAMES = {"close", "claim", "unclaim", "list", "delete"}

MENTION_RE = re.compile(r"^<@([A-Z0-9]+)(?:\|[^>]+)?>$")

app = App(token=os.environ["SLACK_BOT_TOKEN"])
db.init_db()
macros.bootstrap_defaults()

if os.environ.get("INITIAL_ADMIN_ID"):
    helpers.bootstrap_admin(
        os.environ["INITIAL_ADMIN_ID"],
        os.environ.get("INITIAL_ADMIN_ROLE", config.default_role()),
    )

_profile_cache: dict[str, dict] = {}
_setup_code: str | None = None


def setup_code() -> str:
    """One-time code for claiming the first admin from the Home tab. Only
    ever printed to the bot's own logs, so claiming admin takes access to
    the deploy rather than just being first to open the Home tab."""
    global _setup_code
    if _setup_code is None:
        _setup_code = secrets.token_hex(4)
        logging.warning(
            "No admins yet — open the bot's Home tab in Slack, click 'Set up the bot' and enter setup code: %s",
            _setup_code,
        )
    return _setup_code


def signature_role(user_id: str) -> str:
    return helpers.get_role(user_id) or config.default_role()


def _profile(user_id: str) -> dict:
    """Name + avatar for a user, cached for the life of the process since
    they won't change mid-event."""
    if user_id not in _profile_cache:
        profile = app.client.users_info(user=user_id)["user"]["profile"]
        _profile_cache[user_id] = {
            "name": profile.get("first_name") or profile.get("display_name") or profile.get("real_name") or user_id,
            "icon_url": profile.get("image_192") or profile.get("image_72") or profile.get("image_512"),
        }
    return _profile_cache[user_id]


def real_name(user_id: str) -> str:
    """First name if we have one, falling back to display/real name."""
    return _profile(user_id)["name"]


def avatar_url(user_id: str) -> str | None:
    return _profile(user_id)["icon_url"]


def _support_note(client, ticket, text: str) -> None:
    """Post a helper-visible note into the ticket's support-channel thread.
    Never reaches the user's DM."""
    client.chat_postMessage(
        channel=config.support_channel_id(),
        thread_ts=ticket["support_ts"],
        text=f"_— {text}_",
    )


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())


def _user_message_text(user_id: str, text: str, files: list) -> str:
    """How a user's message reads in the support channel: their name and
    mention, then their text quoted (or a note if it was only files)."""
    body = _quote(text) if text.strip() else ""
    if files:
        note = f"_sent {len(files)} attachment(s) — in the thread_"
        body = f"{body}\n{note}" if body else note
    return f"*{real_name(user_id)}* (<@{user_id}>):\n{body}"


def _ticket_blocks(text: str) -> list:
    """The forwarded ticket message, plus Claim/Resolve buttons so helpers
    don't have to type `?claim`/`?close`."""
    if len(text) > 3000:  # section text limit — the full text stays in the message's `text`
        text = text[:2990] + "…"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Claim"}, "action_id": "ticket_claim"},
                {"type": "button", "text": {"type": "plain_text", "text": "Resolve"}, "action_id": "ticket_resolve"},
            ],
        },
    ]


def _copy_files(client, ticket, files: list, channel: str, thread_ts: str, where: str) -> None:
    """Relay attachments into another thread, flagging any that didn't make
    it in the ticket's support thread so nobody assumes they arrived."""
    failed = attachments.relay_files(client, files, channel, thread_ts)
    if failed:
        _support_note(client, ticket, f"Couldn't copy {', '.join(failed)} {where} — check the original message.")


def _close_ticket(client, ticket, resolved_by: str) -> None:
    """Mark a ticket resolved: green tick on the forwarded message + note in
    the support thread. Does not message the user."""
    if ticket["status"] == "open":
        try:
            client.reactions_add(
                channel=config.support_channel_id(), timestamp=ticket["support_ts"], name=RESOLVED_EMOJI
            )
        except Exception:
            pass  # reaction may already exist
        db.set_ticket_status(ticket["support_ts"], "closed", resolved_by=resolved_by)
        _support_note(client, ticket, "This message has been resolved!")
        update_stats(client)


def _claim_ticket(client, ticket, user_id: str) -> None:
    """Record who's handling a ticket. Purely informational for support
    staff — doesn't stop any other helper from replying — and never
    messages the user."""
    db.set_ticket_claim(ticket["support_ts"], user_id)
    _support_note(client, ticket, f"Claimed by <@{user_id}>.")


def _unclaim_ticket(client, ticket, user_id: str) -> None:
    db.clear_ticket_claim(ticket["support_ts"])
    _support_note(client, ticket, f"Unclaimed by <@{user_id}> — up for grabs.")


def _reopen_ticket(client, ticket) -> None:
    """Mark a ticket as needing attention again: drop the green tick and post
    an 'opened again' note in the support thread."""
    if ticket["status"] == "closed":
        try:
            client.reactions_remove(
                channel=config.support_channel_id(), timestamp=ticket["support_ts"], name=RESOLVED_EMOJI
            )
        except Exception:
            pass  # reaction may already be gone
        db.set_ticket_status(ticket["support_ts"], "open")
        _support_note(client, ticket, "This message has been opened again.")
        update_stats(client)


def _refresh_stats(client) -> None:
    channel = config.support_channel_id()
    if not channel:
        return
    open_count = db.count_tickets_by_status("open")
    closed_count = db.count_tickets_by_status("closed")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": home.stats_text(open_count, closed_count)}}]

    old_ts = db.get_setting(STATS_SETTING)
    if old_ts:
        try:
            client.chat_delete(channel=channel, ts=old_ts)
        except Exception as e:
            logging.debug("Couldn't delete previous anchored stats message (ts=%s): %s", old_ts, e)
            try:
                client.chat_update(channel=channel, ts=old_ts, text="Support stats", blocks=blocks)
                return
            except Exception as e2:
                logging.debug("Couldn't update previous stats message (ts=%s): %s", old_ts, e2)

    posted = client.chat_postMessage(channel=channel, text="Support stats", blocks=blocks)
    db.set_setting(STATS_SETTING, posted["ts"])


def update_stats(client) -> None:
    """Delete-and-resend the anchored stats message to keep it pinned to the
    bottom of the support channel."""
    _refresh_stats(client)


def check_unanswered(client) -> None:
    """Have the nudge persona (Chopsticks by default) flag tickets whose user
    has been waiting `stale_days` for a reply — in the ticket's thread, and
    also broadcast to the channel so it's actually seen."""
    days = config.stale_days()
    channel = config.support_channel_id()
    if not days or not channel:
        return
    for ticket in db.list_due_nudges(days):
        who = f"<@{ticket['claimed_by']}> (you claimed this one) — " if ticket["claimed_by"] else ""
        waiting = home.ago(ticket["last_user_at"] or ticket["created_at"])
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=ticket["support_ts"],
                reply_broadcast=True,
                text=(
                    f"{who}*{real_name(ticket['user_id'])}* has been waiting *{waiting}* for a reply here. "
                    "Could someone take a look? :pleading_face:"
                ),
                **config.nudge_identity(),
            )
        except Exception as e:
            logging.warning("Couldn't nudge about ticket %s: %s", ticket["support_ts"], e)
            continue
        db.mark_nudged(ticket["support_ts"])


def _nudge_loop(client, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            check_unanswered(client)
        except Exception:
            logging.exception("Unanswered-ticket check failed")
        stop.wait(NUDGE_CHECK_SECONDS)


@app.event("message")
def handle_message(event, client):
    # Ignore the bot's own messages, edits, joins, etc.
    if event.get("bot_id") or event.get("subtype") not in RELAYED_SUBTYPES:
        return

    if event.get("channel_type") == "im":
        _handle_dm(event, client)
    elif event["channel"] == config.support_channel_id() and event.get("thread_ts"):
        # Top-level channel chatter outside a thread has nothing to relay.
        _handle_support_reply(event, client)


def _handle_dm(event, client) -> None:
    """A DM from a user -> ask if they want it sent to support, or relay a
    follow-up in an existing ticket's DM thread."""
    channel = event["channel"]
    text = event.get("text", "")
    ts = event["ts"]
    user_id = event["user"]
    thread_ts = event.get("thread_ts")
    files = attachments.relayable(event.get("files"))

    # A reply inside an existing ticket's DM thread -> relay it straight
    # into the support thread instead of starting a new confirmation flow
    # (which would otherwise spawn a second thread off this reply's own ts).
    if thread_ts and thread_ts != ts:
        ticket = db.get_ticket_by_dm(channel, thread_ts)
        if ticket is None:
            return  # a reply on the still-pending "send to support?" prompt — nothing to relay yet
        _reopen_ticket(client, ticket)
        db.touch_user_activity(ticket["support_ts"])
        support_channel = config.support_channel_id()
        client.chat_postMessage(
            channel=support_channel,
            thread_ts=ticket["support_ts"],
            text=_user_message_text(user_id, text, files),
            username=real_name(user_id),
            icon_url=avatar_url(user_id),
        )
        _copy_files(client, ticket, files, support_channel, ticket["support_ts"], "from the user's DM")
        client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
        return

    if not config.support_channel_id():
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text="Support isn't quite set up yet — please try again a little later!",
        )
        return

    client.reactions_add(channel=channel, timestamp=ts, name="eyes")

    client.chat_postMessage(
        channel=channel,
        thread_ts=ts,
        text="Want me to send this to the support team?",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Want me to send this to the support team?"},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Yes, send it"},
                        "style": "primary",
                        "action_id": "confirm_send_support",
                        # Just enough to find the message again on confirm —
                        # its text/files are re-fetched then, since a button
                        # value caps out at 2000 chars.
                        "value": json.dumps({"dm_channel": channel, "dm_ts": ts, "user_id": user_id}),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "No thanks"},
                        "action_id": "cancel_send_support",
                    },
                ],
            },
        ],
    )


def _handle_support_reply(event, client) -> None:
    """A "?" reply in a ticket thread -> gate on helper status, maybe relay."""
    channel = event["channel"]
    text = event.get("text", "")
    ts = event["ts"]
    user_id = event["user"]

    if not text.startswith("?"):
        return  # internal chatter in a thread, not meant for the user

    if not helpers.is_helper(user_id):
        client.reactions_add(channel=channel, timestamp=ts, name="x")
        client.chat_postEphemeral(
            channel=channel,
            user=user_id,
            text="You don't have permission to post here — ping an admin if you think that's wrong.",
        )
        return

    ticket = db.get_ticket_by_support_ts(event["thread_ts"])
    if ticket is None:
        return  # reply on a thread we don't recognise as a ticket

    reply_text = text[1:].strip()
    files = attachments.relayable(event.get("files"))
    command = reply_text.lower()

    # `?close` resolves the ticket without relaying anything to the user.
    if command == "close":
        _close_ticket(client, ticket, resolved_by=user_id)
        return

    # `?claim`/`?unclaim` are backend-only bookkeeping for support staff — no DM.
    if command == "claim":
        _claim_ticket(client, ticket, user_id)
        return
    if command == "unclaim":
        _unclaim_ticket(client, ticket, user_id)
        return

    if not reply_text and not files:
        return  # a bare "?" — nothing to send

    _reopen_ticket(client, ticket)
    macro_text = macros.get_macro(reply_text) if reply_text else None
    if macro_text is not None:
        macro_name = reply_text
        reply_text = macro_text
        client.chat_postMessage(
            channel=channel,
            thread_ts=ticket["support_ts"],
            text=f"sent macro `{macro_name}` to <@{ticket['user_id']}>. the macro says: `{macro_text}`",
        )

    body = reply_text or f"_Sent you {len(files)} attachment(s) — below._"
    sent = client.chat_postMessage(
        channel=ticket["dm_channel"],
        thread_ts=ticket["dm_ts"],
        text=f"{body}\n\n_— <@{user_id}>, {signature_role(user_id)}_",
        username=real_name(user_id),
        icon_url=avatar_url(user_id),
    )
    db.create_reply(ts, ticket["id"], ticket["dm_channel"], sent["ts"], user_id, body)
    db.touch_helper_activity(ticket["support_ts"])
    _copy_files(client, ticket, files, ticket["dm_channel"], ticket["dm_ts"], "to the user")
    client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")


def _fetch_dm_message(client, dm_channel: str, dm_ts: str) -> dict | None:
    try:
        history = client.conversations_history(channel=dm_channel, latest=dm_ts, inclusive=True, limit=1)
    except Exception as e:
        logging.warning("Couldn't fetch DM message %s: %s", dm_ts, e)
        return None
    return next((m for m in history.get("messages", []) if m.get("ts") == dm_ts), None)


def _replace_prompt(client, body, text: str) -> None:
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=text,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


@app.action("confirm_send_support")
def handle_confirm(ack, body, client):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    dm_channel = payload["dm_channel"]
    dm_ts = payload["dm_ts"]
    user_id = payload["user_id"]

    # A double-click mustn't open a second ticket.
    if db.get_ticket_by_dm(dm_channel, dm_ts) is not None:
        _replace_prompt(client, body, "Sent to the support team ✅")
        return

    support_channel = config.support_channel_id()
    if not support_channel:
        _replace_prompt(client, body, "Support isn't quite set up yet — please try again a little later!")
        return

    message = _fetch_dm_message(client, dm_channel, dm_ts)
    if message is None and "text" not in payload:
        _replace_prompt(client, body, "I couldn't find that message any more — was it deleted? Try sending it again.")
        return
    # Prompts posted before attachments support carried the text in the button.
    original_text = message.get("text", "") if message else payload["text"]
    files = attachments.relayable(message.get("files")) if message else []

    text = _user_message_text(user_id, original_text, files)
    posted = client.chat_postMessage(
        channel=support_channel,
        text=text,
        blocks=_ticket_blocks(text),
        username=real_name(user_id),
        icon_url=avatar_url(user_id),
    )
    db.create_ticket(posted["ts"], dm_channel, dm_ts, user_id, original_text)
    if files:
        ticket = db.get_ticket_by_support_ts(posted["ts"])
        _copy_files(client, ticket, files, support_channel, posted["ts"], "from the user's DM")

    client.reactions_remove(channel=dm_channel, timestamp=dm_ts, name="eyes")
    client.reactions_add(channel=dm_channel, timestamp=dm_ts, name="white_check_mark")

    _replace_prompt(client, body, "Sent to the support team ✅")

    update_stats(client)


@app.action("cancel_send_support")
def handle_cancel(ack, body, client):
    ack()
    _replace_prompt(client, body, "No worries — not sent.")


def _ticket_from_button(client, body):
    """The ticket behind a Claim/Resolve button, or None (after telling the
    clicker why) if they can't use it."""
    user_id = body["user"]["id"]
    if not helpers.is_helper(user_id):
        client.chat_postEphemeral(channel=body["channel"]["id"], user=user_id, text="Only helpers can do that.")
        return None
    return db.get_ticket_by_support_ts(body["message"]["ts"])


@app.action("ticket_claim")
def handle_ticket_claim(ack, body, client):
    ack()
    ticket = _ticket_from_button(client, body)
    if ticket is not None:
        _claim_ticket(client, ticket, body["user"]["id"])


@app.action("ticket_resolve")
def handle_ticket_resolve(ack, body, client):
    ack()
    ticket = _ticket_from_button(client, body)
    if ticket is None:
        return
    if ticket["status"] == "closed":
        client.chat_postEphemeral(channel=body["channel"]["id"], user=body["user"]["id"], text="Already resolved.")
        return
    _close_ticket(client, ticket, resolved_by=body["user"]["id"])


def _can_manage_reply(user_id: str, reply) -> bool:
    return helpers.is_admin(user_id) or user_id == reply["author_id"]


@app.shortcut("edit_reply")
def handle_edit_reply_shortcut(ack, shortcut, client):
    ack()
    channel_id = shortcut["channel"]["id"]
    if channel_id != config.support_channel_id():
        return

    support_reply_ts = shortcut["message"]["ts"]
    caller_id = shortcut["user"]["id"]

    reply = db.get_reply_by_support_ts(support_reply_ts)
    if reply is None:
        client.chat_postEphemeral(channel=channel_id, user=caller_id, text="That's not a tracked support reply.")
        return
    if reply["deleted"]:
        client.chat_postEphemeral(channel=channel_id, user=caller_id, text="That reply was already deleted.")
        return
    if not _can_manage_reply(caller_id, reply):
        client.chat_postEphemeral(
            channel=channel_id, user=caller_id, text="Only the sender or an admin can edit this reply."
        )
        return

    client.views_open(
        trigger_id=shortcut["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "edit_reply_submit",
            "private_metadata": json.dumps({"support_reply_ts": support_reply_ts}),
            "title": {"type": "plain_text", "text": "Edit reply"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "text_block",
                    "label": {"type": "plain_text", "text": "Message sent to the user"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "text_input",
                        "multiline": True,
                        "initial_value": reply["text"],
                    },
                }
            ],
        },
    )


@app.view("edit_reply_submit")
def handle_edit_reply_submit(ack, body, client, view):
    ack()
    support_reply_ts = json.loads(view["private_metadata"])["support_reply_ts"]
    new_text = view["state"]["values"]["text_block"]["text_input"]["value"].strip()

    reply = db.get_reply_by_support_ts(support_reply_ts)
    if reply is None or reply["deleted"]:
        return  # deleted or vanished between opening the modal and submitting it

    caller_id = body["user"]["id"]
    if not _can_manage_reply(caller_id, reply):
        return

    client.chat_update(
        channel=reply["dm_channel"],
        ts=reply["dm_ts"],
        text=f"{new_text}\n\n_— <@{reply['author_id']}>, {signature_role(reply['author_id'])}_",
    )
    db.update_reply_text(support_reply_ts, new_text)
    client.reactions_add(channel=config.support_channel_id(), timestamp=support_reply_ts, name="pencil2")


@app.shortcut("delete_reply")
def handle_delete_reply_shortcut(ack, shortcut, client):
    ack()
    channel_id = shortcut["channel"]["id"]
    if channel_id != config.support_channel_id():
        return

    support_reply_ts = shortcut["message"]["ts"]
    caller_id = shortcut["user"]["id"]

    reply = db.get_reply_by_support_ts(support_reply_ts)
    if reply is None:
        client.chat_postEphemeral(channel=channel_id, user=caller_id, text="That's not a tracked support reply.")
        return
    if reply["deleted"]:
        client.chat_postEphemeral(channel=channel_id, user=caller_id, text="That reply was already deleted.")
        return
    if not _can_manage_reply(caller_id, reply):
        client.chat_postEphemeral(
            channel=channel_id, user=caller_id, text="Only the sender or an admin can delete this reply."
        )
        return

    client.chat_delete(channel=reply["dm_channel"], ts=reply["dm_ts"])
    db.mark_reply_deleted(support_reply_ts)

    try:
        client.reactions_remove(channel=channel_id, timestamp=support_reply_ts, name="white_check_mark")
    except Exception:
        pass  # reaction may already be gone (e.g. an edit removed it)
    client.reactions_add(channel=channel_id, timestamp=support_reply_ts, name="x")


HELP_TEXT = """*Support bot commands*
• `/support whoami` — your own role
• `/support list` — the helper roster _(helpers)_
• `/support unanswered` — open tickets still waiting on a reply _(helpers)_
• `/support macros` — every macro and what it says _(helpers)_
• `/support macros <name> <reply text>` — add/update a macro _(admins)_
• `/support macros delete <name>` — delete a macro _(admins)_
• `/support promote @user [role]` · `/support superpromote @user [role]` · `/support demote @user` _(admins)_
• `/support config` — open the settings _(admins)_

*In a ticket thread* _(helpers)_
• `?your reply` — send a reply to the user (attach files to send those too)
• `?macro-name` — send a macro's canned reply
• `?claim` / `?unclaim` — mark yourself as handling it, or let it go
• `?close` — resolve it without messaging the user

Admins can also manage everything from the bot's *Home* tab."""


def _format_macros() -> str:
    roster = macros.all_macros()
    if not roster:
        return "No macros configured yet."
    lines = [f"*Macros ({len(roster)})* — send one with `?name` in a ticket thread:"]
    for name in sorted(roster):
        lines.append(f"• `?{name}`\n{_quote(roster[name])}")
    return "\n".join(lines)


def _valid_macro_name(name: str) -> str | None:
    """Why `name` can't be a macro name, or None if it's fine."""
    if not name:
        return "Macro names can't be empty."
    if any(c.isspace() for c in name):
        return "Macro names can't contain spaces — try dashes instead."
    if name in RESERVED_MACRO_NAMES:
        return f"`?{name}` is already a bot command — pick another name."
    return None


@app.command("/support")
def handle_support_command(ack, respond, command, client):
    ack()
    caller_id = command["user_id"]
    parts = command.get("text", "").strip().split(maxsplit=2)

    usage = "Usage: `/support help` for every command."
    if not parts or parts[0].lower() == "help":
        respond(HELP_TEXT)
        return

    subcommand = parts[0].lower()

    if subcommand == "whoami":
        role = helpers.get_role(caller_id)
        if role is None:
            respond("You're not a registered helper.")
        else:
            admin_note = " (admin)" if helpers.is_admin(caller_id) else ""
            respond(f"You're registered as *{role}*{admin_note}.")
        return

    if subcommand == "list":
        if not helpers.is_helper(caller_id):
            respond("Only helpers can use this command.")
            return
        roster = helpers.all_helpers()
        if not roster:
            respond("No helpers registered yet.")
            return
        lines = [
            f"• <@{uid}> — {info.get('role', config.default_role())}{' (admin)' if info.get('admin') else ''}"
            for uid, info in roster.items()
        ]
        respond("\n".join(lines))
        return

    if subcommand == "unanswered":
        if not helpers.is_helper(caller_id):
            respond("Only helpers can use this command.")
            return
        lines = home.unanswered_lines(limit=25)
        if not lines:
            respond(":white_check_mark: Nothing's waiting — every open ticket has a reply.")
            return
        total = db.count_unanswered()
        heading = f":hourglass_flowing_sand: *Waiting on a reply ({total})*, longest first:"
        respond(heading + "\n" + "\n".join(lines))
        return

    if subcommand == "macros":
        if not helpers.is_helper(caller_id):
            respond("Only helpers can use this command.")
            return

        if len(parts) == 1 or parts[1].lower() == "list":
            respond(_format_macros())
            return

        if not helpers.is_admin(caller_id):
            respond("Only admins can add, update or delete macros.")
            return

        if parts[1].lower() == "delete":
            if len(parts) < 3:
                respond("Usage: `/support macros delete <name>`")
                return
            name = parts[2].strip().lstrip("?").lower()
            if macros.delete_macro(name):
                respond(f"Macro `?{name}` deleted.")
            else:
                respond(f"There's no macro called `?{name}`.")
            return

        if len(parts) < 3:
            respond("Usage: `/support macros <name> <reply text>` to add/update, or `/support macros` to list.")
            return
        macro_name = parts[1].strip().lstrip("?").lower()
        problem = _valid_macro_name(macro_name)
        if problem:
            respond(problem)
            return
        macros.add_macro(macro_name, parts[2].strip())
        respond(f"Macro `?{macro_name}` saved.")
        return

    if subcommand == "config":
        if not helpers.is_admin(caller_id):
            respond("Only admins can change the bot's settings.")
            return
        client.views_open(trigger_id=command["trigger_id"], view=home.config_modal())
        return

    if subcommand in ("promote", "superpromote", "demote"):
        if not helpers.is_admin(caller_id):
            respond(f"Only admins can {subcommand} helpers.")
            return
        if len(parts) < 2:
            respond(usage)
            return
        match = MENTION_RE.match(parts[1])
        if not match:
            respond(f"Couldn't find a user to {subcommand} — mention them like `@user`.")
            return
        target_id = match.group(1)

        if subcommand == "demote":
            if helpers.is_last_admin(target_id):
                respond(f"<@{target_id}> is the only admin — make someone else an admin first.")
            elif helpers.remove_helper(target_id):
                respond(f"<@{target_id}> is no longer a helper.")
            else:
                respond(f"<@{target_id}> wasn't a helper.")
            return

        as_admin = subcommand == "superpromote"
        role = parts[2].strip() if len(parts) > 2 else config.default_role()
        helpers.add_helper(target_id, role, admin=as_admin)
        admin_note = " (admin)" if as_admin else ""
        respond(f"<@{target_id}> is now a helper: *{role}*{admin_note}.")
        return

    respond(usage)


# --- App Home --------------------------------------------------------------


def publish_home(client, user_id: str) -> None:
    client.views_publish(user_id=user_id, view=home.build_home_view(user_id))


@app.event("app_home_opened")
def handle_app_home_opened(event, client):
    if event.get("tab") != "home":
        return  # ignore the Messages tab
    publish_home(client, event["user"])


@app.action("home_refresh")
def handle_home_refresh(ack, body, client):
    ack()
    publish_home(client, body["user"]["id"])


def _admin_action(client, body) -> bool:
    """Gate a Home-tab admin button. A non-admin can only hit one through a
    stale view (e.g. they've just been demoted), so refresh it for them."""
    user_id = body["user"]["id"]
    if helpers.is_admin(user_id):
        return True
    publish_home(client, user_id)
    return False


@app.action("open_setup")
def handle_open_setup(ack, body, client):
    ack()
    if helpers.all_helpers():
        publish_home(client, body["user"]["id"])  # someone else got there first
        return
    setup_code()  # make sure there's a code in the logs to enter
    client.views_open(trigger_id=body["trigger_id"], view=home.setup_modal())


@app.view("setup_submit")
def handle_setup_submit(ack, body, client, view):
    values = view["state"]["values"]
    code = values["code"]["value"]["value"].strip()
    role = values["role"]["value"]["value"].strip()
    if helpers.all_helpers():
        ack(response_action="errors", errors={"code": "The bot's already set up — ask an admin to add you."})
        return
    if not secrets.compare_digest(code, setup_code()):
        ack(response_action="errors", errors={"code": "That's not the setup code from the bot's logs."})
        return

    user_id = body["user"]["id"]
    helpers.add_helper(user_id, role, admin=True)
    logging.info("%s claimed admin via the setup code", user_id)
    # Straight on to the settings, since that's the next thing to do.
    ack(response_action="update", view=home.config_modal())
    publish_home(client, user_id)


@app.action("open_config")
def handle_open_config(ack, body, client):
    ack()
    if _admin_action(client, body):
        client.views_open(trigger_id=body["trigger_id"], view=home.config_modal())


@app.view("config_submit")
def handle_config_submit(ack, body, client, view):
    user_id = body["user"]["id"]
    if not helpers.is_admin(user_id):
        ack()
        return
    values = view["state"]["values"]
    event_name = values["event_name"]["value"]["value"].strip()
    channel = values["support_channel_id"]["value"]["selected_conversation"]
    stale_days = values["stale_days"]["value"]["value"] or "0"
    nudge_name = values["nudge_name"]["value"]["value"].strip()
    nudge_icon = values["nudge_icon"]["value"]["value"].strip()

    old_channel = config.support_channel_id()
    if channel != old_channel:
        # Doubles as the membership check: posting fails if the bot isn't in it.
        try:
            client.chat_postMessage(
                channel=channel, text=f":wave: This is now the {event_name} support channel — set by <@{user_id}>."
            )
        except Exception as e:
            logging.info("Couldn't post to new support channel %s: %s", channel, e)
            ack(
                response_action="errors",
                errors={"support_channel_id": "I can't post there — invite me to it first (/invite), then retry."},
            )
            return
    ack()

    config.save("event_name", event_name)
    config.save("stale_days", stale_days)
    config.save("nudge_name", nudge_name)
    config.save("nudge_icon", nudge_icon)
    if channel != old_channel:
        # Move the pinned stats message over to the new channel.
        old_stats_ts = db.get_setting(STATS_SETTING)
        if old_channel and old_stats_ts:
            try:
                client.chat_delete(channel=old_channel, ts=old_stats_ts)
            except Exception as e:
                logging.debug("Couldn't delete stats message in the old channel: %s", e)
        db.set_setting(STATS_SETTING, "")
        config.save("support_channel_id", channel)
        update_stats(client)
    publish_home(client, user_id)


@app.action("open_add_helper")
def handle_open_add_helper(ack, body, client):
    ack()
    if _admin_action(client, body):
        client.views_open(trigger_id=body["trigger_id"], view=home.helper_modal())


@app.action("helper_menu")
def handle_helper_menu(ack, body, client):
    ack()
    if not _admin_action(client, body):
        return
    choice, uid = body["actions"][0]["selected_option"]["value"].split(":", 1)
    if choice == "edit":
        view = home.helper_modal(uid)
    else:
        view = home.confirm_modal(
            "remove_helper_submit",
            "Remove helper",
            f"Remove <@{uid}> from the helper roster? They'll lose access to replying to tickets.",
            "Remove",
            {"user_id": uid},
        )
    client.views_open(trigger_id=body["trigger_id"], view=view)


@app.view("helper_submit")
def handle_helper_submit(ack, body, client, view):
    caller_id = body["user"]["id"]
    if not helpers.is_admin(caller_id):
        ack()
        return
    values = view["state"]["values"]
    target_id = values["user"]["value"]["selected_user"]
    role = values["role"]["value"]["value"].strip()
    as_admin = bool(values["admin"]["value"].get("selected_options"))
    editing = json.loads(view["private_metadata"]).get("editing")

    # Changing the person on an edit moves the entry, so the old one goes.
    moved_from = editing if editing and editing != target_id else None
    if not as_admin and helpers.is_last_admin(target_id):
        ack(response_action="errors", errors={"admin": "They're the only admin — make someone else an admin first."})
        return
    if moved_from and helpers.is_last_admin(moved_from) and not as_admin:
        ack(response_action="errors", errors={"user": "That would remove the only admin."})
        return
    ack()

    if moved_from:
        helpers.remove_helper(moved_from)
    helpers.set_helper(target_id, role, admin=as_admin)
    publish_home(client, caller_id)


@app.view("remove_helper_submit")
def handle_remove_helper_submit(ack, body, client, view):
    caller_id = body["user"]["id"]
    target_id = json.loads(view["private_metadata"])["user_id"]
    if helpers.is_admin(caller_id) and helpers.is_last_admin(target_id):
        ack(
            response_action="update",
            view=home.confirm_modal(
                "noop", "Remove helper", f"<@{target_id}> is the only admin — make someone else one first.", "OK", {}
            ),
        )
        return
    ack()
    if helpers.is_admin(caller_id):
        helpers.remove_helper(target_id)
        publish_home(client, caller_id)


@app.view("noop")
def handle_noop(ack):
    ack()


@app.action("open_add_macro")
def handle_open_add_macro(ack, body, client):
    ack()
    if _admin_action(client, body):
        client.views_open(trigger_id=body["trigger_id"], view=home.macro_modal())


@app.action("macro_menu")
def handle_macro_menu(ack, body, client):
    ack()
    if not _admin_action(client, body):
        return
    choice, name = body["actions"][0]["selected_option"]["value"].split(":", 1)
    if choice == "edit":
        view = home.macro_modal(name)
    else:
        view = home.confirm_modal(
            "delete_macro_submit", "Delete macro", f"Delete the `?{name}` macro?", "Delete", {"name": name}
        )
    client.views_open(trigger_id=body["trigger_id"], view=view)


@app.view("macro_submit")
def handle_macro_submit(ack, body, client, view):
    caller_id = body["user"]["id"]
    if not helpers.is_admin(caller_id):
        ack()
        return
    values = view["state"]["values"]
    name = values["name"]["value"]["value"].strip().lstrip("?").lower()
    text = values["text"]["value"]["value"].strip()
    problem = _valid_macro_name(name)
    if problem:
        ack(response_action="errors", errors={"name": problem})
        return
    ack()

    editing = json.loads(view["private_metadata"]).get("editing")
    if editing and editing != name:
        macros.delete_macro(editing)  # renamed
    macros.add_macro(name, text)
    publish_home(client, caller_id)


@app.view("delete_macro_submit")
def handle_delete_macro_submit(ack, body, client, view):
    ack()
    caller_id = body["user"]["id"]
    if helpers.is_admin(caller_id):
        macros.delete_macro(json.loads(view["private_metadata"])["name"])
        publish_home(client, caller_id)


if __name__ == "__main__":
    if not helpers.all_helpers():
        setup_code()

    if config.support_channel_id():
        try:
            update_stats(app.client)
        except Exception as e:
            logging.warning("Couldn't refresh support stats on startup: %s", e)

        try:
            app.client.chat_postMessage(
                channel=config.support_channel_id(),
                text=f"{config.event_name()} support bot has been deployed successfully.",
            )
        except Exception as e:
            logging.warning("Couldn't post startup message to support channel: %s", e)
    else:
        logging.warning("No support channel configured yet — an admin can pick one from the bot's Home tab.")

    threading.Thread(target=_nudge_loop, args=(app.client, threading.Event()), daemon=True).start()

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Support bot running (Socket Mode)...")
    handler.start()
