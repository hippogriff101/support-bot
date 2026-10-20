import json
import logging
import os
import re
import secrets
import threading
import time

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import attachments
import config
import db
import helpers
import home
import macros
import scheduling

load_dotenv()
logging.basicConfig(level=logging.INFO)

STATS_SETTING = "anchored_stats_ts"
RESOLVED_EMOJI = "ms-green-tick"
NUDGE_CHECK_SECONDS = 10 * 60
# How often to look for scheduled `?@user` DMs that are due. A minute is fine:
# nothing here is more time-critical than "sometime around then".
SCHEDULER_CHECK_SECONDS = 60
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


def _button(text: str, action_id: str, style: str | None = None) -> dict:
    button = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id}
    if style:
        button["style"] = style
    return button


def _ticket_blocks(text: str, ticket=None) -> list:
    """The forwarded ticket message plus buttons for whatever can happen to
    it next, so helpers don't have to type `?claim`/`?close`:
      open, unclaimed -> Claim + Resolve
      open, claimed   -> "Claimed by" line + Unclaim + Resolve
      resolved        -> "Resolved by" line + Reopen
    `ticket=None` is a brand-new (open, unclaimed) ticket.

    A channel message's buttons are shared by everyone who can see it, so the
    state they show is the ticket's, not the viewer's — the handlers do the
    per-person gating (an ephemeral "X already has this one" for anyone but
    the claimer)."""
    if len(text) > 3000:  # section text limit — the full text stays in the message's `text`
        text = text[:2990] + "…"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    status = ticket["status"] if ticket else "open"
    claimed_by = ticket["claimed_by"] if ticket else None
    if status == "closed":
        by = f" by <@{ticket['resolved_by']}>" if ticket["resolved_by"] else ""
        state, buttons = f":ms-green-tick: Resolved{by}", [_button("Reopen", "ticket_reopen")]
    elif claimed_by:
        state = f":raising_hand: Claimed by <@{claimed_by}>"
        buttons = [_button("Unclaim", "ticket_unclaim"), _button("Resolve", "ticket_resolve", "primary")]
    else:
        state, buttons = None, [_button("Claim", "ticket_claim"), _button("Resolve", "ticket_resolve", "primary")]

    if state:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": state}]})
    blocks.append({"type": "actions", "elements": buttons})
    return blocks


def refresh_ticket_message(client, support_ts: str) -> None:
    """Re-render a ticket's forwarded message so its buttons match its
    current state. Tickets from before support_text was stored get their
    text read back from Slack once."""
    ticket = db.get_ticket_by_support_ts(support_ts)
    if ticket is None:
        return
    channel = config.support_channel_id()
    if not channel:
        return
    text = ticket["support_text"]
    try:
        if text is None:
            parent = client.conversations_replies(channel=channel, ts=support_ts, limit=1)["messages"][0]
            text = parent.get("text", "")
            db.set_ticket_text(support_ts, text)
        client.chat_update(channel=channel, ts=support_ts, text=text, blocks=_ticket_blocks(text, ticket))
    except Exception as e:
        logging.warning("Couldn't refresh ticket message %s: %s", support_ts, e)


def _log(client, ticket, text: str) -> None:
    """An audit-log line in the ticket's support thread, posted as the nudge
    persona (Chopsticks) so it reads as the bot keeping records rather than
    as a person talking."""
    client.chat_postMessage(
        channel=config.support_channel_id(),
        thread_ts=ticket["support_ts"],
        text=text,
        **config.nudge_identity(),
    )


def _who(user_id: str) -> str:
    """`*@Name*` — names someone in a log line without pinging them."""
    return f"*@{real_name(user_id)}*"


def _stamp(ts: str | None) -> str:
    """A Slack ts (`1789900660.486449`) as `2026-10-20 14:23 UTC` for log
    lines. Falls back to the raw value if it's in any other shape."""
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(str(ts).split(".")[0])))
    except (TypeError, ValueError):
        return str(ts or "?")


def _now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


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
        refresh_ticket_message(client, ticket["support_ts"])
        update_stats(client)


def _claim_ticket(client, ticket, user_id: str) -> None:
    """Record who's handling a ticket. Purely informational for support
    staff — doesn't stop any other helper from replying — and never
    messages the user."""
    db.set_ticket_claim(ticket["support_ts"], user_id)
    _support_note(client, ticket, f"Claimed by <@{user_id}>.")
    refresh_ticket_message(client, ticket["support_ts"])


def _unclaim_ticket(client, ticket, user_id: str) -> None:
    db.clear_ticket_claim(ticket["support_ts"])
    _support_note(client, ticket, f"Unclaimed by <@{user_id}> — up for grabs.")
    refresh_ticket_message(client, ticket["support_ts"])


def _reopen_ticket(client, ticket, by: str | None = None) -> None:
    """Mark a ticket as needing attention again: drop the green tick and post
    an 'opened again' note in the support thread. `by` is the helper who hit
    Reopen; automatic reopens (new activity on the ticket) leave it out."""
    if ticket["status"] == "closed":
        try:
            client.reactions_remove(
                channel=config.support_channel_id(), timestamp=ticket["support_ts"], name=RESOLVED_EMOJI
            )
        except Exception:
            pass  # reaction may already be gone
        db.set_ticket_status(ticket["support_ts"], "open")
        _support_note(client, ticket, f"Reopened by <@{by}>." if by else "This message has been opened again.")
        refresh_ticket_message(client, ticket["support_ts"])
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


# --- Timed sends: `?@user` messages -------------------------------------------


def send_due_scheduled_dms(client) -> None:
    """Post any helper message whose time has come, signed with the helper who
    wrote it (and with their `{name}`/`{user-at}` placeholders filled in), in
    the DM thread its `?@user` ticket hangs off."""
    for row in db.list_due_scheduled_dms():
        ticket = db.get_ticket_by_support_ts(row["support_ts"]) if row["support_ts"] else None
        try:
            body = scheduling.render(
                row["body"],
                user_id=row["recipient_id"],
                name=real_name(row["recipient_id"]),
                helper_id=row["helper_id"],
            )
            sent = client.chat_postMessage(
                channel=row["dm_channel"],
                thread_ts=row["thread_ts"] or None,
                text=f"{body}\n\n_— <@{row['helper_id']}>, {signature_role(row['helper_id'])}_",
                username=real_name(row["helper_id"]),
                icon_url=avatar_url(row["helper_id"]),
            )
            db.mark_scheduled_dm_sent(row["id"], sent["ts"])
        except Exception as e:
            logging.warning("Couldn't send scheduled DM %s: %s", row["id"], e)
            db.mark_scheduled_dm_failed(row["id"])
            if ticket is not None:
                _support_note(
                    client,
                    ticket,
                    f"<@{row['helper_id']}> your scheduled message to <@{row['recipient_id']}> couldn't be sent "
                    f"({e}). Reply here with `?…` to try again.",
                )
            continue
        if ticket is not None:
            db.touch_helper_activity(ticket["support_ts"])
            _support_note(client, ticket, f"First message sent to <@{row['recipient_id']}>.")


def _scheduler_loop(client, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            send_due_scheduled_dms(client)
        except Exception:
            logging.exception("Scheduled-DM send failed")
        stop.wait(SCHEDULER_CHECK_SECONDS)


@app.event("message")
def handle_message(event, client):
    subtype = event.get("subtype")
    is_dm = event.get("channel_type") == "im"
    in_support = event["channel"] == config.support_channel_id()

    if subtype in ("message_changed", "message_deleted"):
        if is_dm or in_support:
            _handle_edit_or_delete(event, client, is_dm)
        return

    # Ignore the bot's own messages, joins, etc.
    if event.get("bot_id") or subtype not in RELAYED_SUBTYPES:
        return

    if is_dm:
        _handle_dm(event, client)
    elif in_support:
        # A reply in a ticket thread, or a `?@user` typed as its own message in
        # the channel — which starts a thread rather than joining one.
        _handle_support_reply(event, client)


def _yes_no(text: str, yes_action: str, no_action: str, value: dict, files: str = "") -> list:
    """A "do you want me to send this?" prompt. `files` is a description of
    what's being sent (`<url|name>` lines) shown above the buttons."""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if files:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": files}]})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {**_button("Yes, send it", yes_action, "primary"), "value": json.dumps(value)},
                {**_button("No, don't send", no_action), "value": json.dumps(value)},
            ],
        }
    )
    return blocks


def _relay_user_followup(client, ticket, user_id: str, text: str, files: list, dm_channel: str, dm_ts: str):
    """Copy a user's follow-up in their ticket's DM thread into the support
    thread, remembering where it went so edits/deletes can follow it."""
    _reopen_ticket(client, ticket)
    db.touch_user_activity(ticket["support_ts"])
    support_channel = config.support_channel_id()
    copy = client.chat_postMessage(
        channel=support_channel,
        thread_ts=ticket["support_ts"],
        text=_user_message_text(user_id, text, files),
        username=real_name(user_id),
        icon_url=avatar_url(user_id),
    )
    db.create_user_message(dm_channel, dm_ts, ticket["id"], copy["ts"])
    _copy_files(client, ticket, files, support_channel, ticket["support_ts"], "from the user's DM")
    client.reactions_add(channel=dm_channel, timestamp=dm_ts, name="white_check_mark")


def _render_reply(ticket, helper_id: str, text: str) -> str:
    """Fill a reply's `{name}`-style placeholders for the ticket's user. Used
    both when a reply is sent and when an edit is mirrored, so the stored text
    and an edit of it are always compared like for like."""
    if not text:
        return text
    return scheduling.render(text, user_id=ticket["user_id"], name=real_name(ticket["user_id"]), helper_id=helper_id)


def _send_helper_reply(client, ticket, user_id: str, support_msg_ts: str, reply_text: str, files: list) -> None:
    """Relay a helper's reply (text and/or files) into a user's DM, signed with
    the helper's role."""
    _reopen_ticket(client, ticket)
    body = _render_reply(ticket, user_id, reply_text) or f"_Sent you {len(files)} attachment(s) — below._"
    sent = client.chat_postMessage(
        channel=ticket["dm_channel"],
        thread_ts=ticket["dm_ts"],
        text=f"{body}\n\n_— <@{user_id}>, {signature_role(user_id)}_",
        username=real_name(user_id),
        icon_url=avatar_url(user_id),
    )
    db.create_reply(support_msg_ts, ticket["id"], ticket["dm_channel"], sent["ts"], user_id, body)
    db.touch_helper_activity(ticket["support_ts"])
    _copy_files(client, ticket, files, ticket["dm_channel"], ticket["dm_ts"], "to the user")
    if config.support_channel_id():
        client.reactions_add(channel=config.support_channel_id(), timestamp=support_msg_ts, name="white_check_mark")


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
            return  # e.g. a reply on the still-pending "send to support?" prompt
        if files:
            # Uploads are easy to send by accident (wrong screenshot, private
            # info), so check before they reach the support team — and show
            # the files themselves so they can see exactly what's going.
            db.add_outbox(thread_ts, ts, user_id, attachments.summary(files))
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Want me to send this to the support team?",
                blocks=_yes_no(
                    f"Want me to send {'this and ' if text.strip() else ''}your {len(files)} attachment(s) "
                    "to the support team?",
                    "confirm_user_files",
                    "cancel_user_files",
                    {"thread_ts": thread_ts, "msg_ts": ts, "asker": user_id},
                    files=attachments.describe(files),
                ),
            )
            return
        _relay_user_followup(client, ticket, user_id, text, files, channel, ts)
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
    """A `?` message in the support channel -> gate on helper status, maybe relay.

    Covers a reply inside a ticket thread and a `?@user` posted as its own
    message in the channel, so the ticket lookup can legitimately come back
    empty.
    """
    channel = event["channel"]
    text = event.get("text", "")
    ts = event["ts"]
    user_id = event["user"]
    files = attachments.relayable(event.get("files"))
    thread_ts = event.get("thread_ts")

    if not text.startswith("?"):
        # Internal chatter, not meant for the user — but a helper dropping a
        # file in might well mean it for them, so offer (just to them) to send
        # it on. Outside a ticket thread there's nobody to send it to.
        if files and helpers.is_helper(user_id) and thread_ts and db.get_ticket_by_support_ts(thread_ts):
            db.add_outbox(thread_ts, ts, user_id, attachments.summary(files))
            client.chat_postEphemeral(
                channel=channel,
                thread_ts=thread_ts,
                user=user_id,
                text="Do you want to send this to the user?",
                blocks=_yes_no(
                    f"Do you want to send your {len(files)} attachment(s) to the user?",
                    "confirm_helper_files",
                    "cancel_helper_files",
                    {"thread_ts": thread_ts, "msg_ts": ts, "asker": user_id},
                    files=attachments.describe(files),
                ),
            )
        return

    if not helpers.is_helper(user_id):
        client.reactions_add(channel=channel, timestamp=ts, name="x")
        client.chat_postEphemeral(
            channel=channel,
            thread_ts=thread_ts,
            user=user_id,
            text="You don't have permission to post here — ping an admin if you think that's wrong.",
        )
        return

    reply_text = text[1:].strip()

    # `?@user` / `?<@U123>` / `? U123` is addressed to somebody else, so it
    # stands on its own as a channel message — there needn't be a ticket
    # behind it for this to work.
    target = _mention_target(reply_text)
    if target:
        _start_dm_setup(client, db.get_ticket_by_support_ts(thread_ts) if thread_ts else None, ts, user_id, target)
        return

    ticket = db.get_ticket_by_support_ts(thread_ts) if thread_ts else None
    if ticket is None:
        return  # reply in a thread we don't recognise as a ticket

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

    macro_text = macros.get_macro(reply_text) if reply_text else None
    if macro_text is not None:
        macro_name = reply_text
        reply_text = macro_text
        client.chat_postMessage(
            channel=channel,
            thread_ts=ticket["support_ts"],
            text=f"sent macro `{macro_name}` to <@{ticket['user_id']}>. the macro says: `{macro_text}`",
        )

    _send_helper_reply(client, ticket, user_id, ts, reply_text, files)


# --- Setting up a DM with someone else (`?@user`) ---------------------------

# After a `?`, a mention of somebody: `<@U123>`, `<@U123|name>` (what Slack
# turns a typed @mention into), or `@U123`. The `@` is required — without it
# an ordinary reply like `?WAIT` or `?UPDATE` would read as a user id. The `?`
# itself may or may not have a space.
_MENTION_TARGET_RE = re.compile(r"^(?:<@([UW][A-Z0-9]+)(?:\|[^>]*)?>|@([UW][A-Z0-9]+))$")


def _mention_target(text: str) -> str | None:
    """The user a `?@user` targets, or None if this is an ordinary reply."""
    match = _MENTION_TARGET_RE.match(text.strip())
    if not match:
        return None
    return match.group(1) or match.group(2)


def _start_dm_setup(client, ticket, msg_ts: str, helper_id: str, target_id: str) -> None:
    """The "setting up a message thread" prompt, threaded under the message the
    helper typed `?@user` on.

    `ticket` is the ticket that message was in, or None when it was posted as a
    message of its own. The prompt belongs to the `?@user` line either way, so
    it reads as a reply to what was actually typed rather than to whatever
    ticket happened to be nearby — and the confirm step carries the ticket
    along only as the place to log it.
    """
    if target_id == helper_id:
        _support_note(client, ticket, "You can't set up a DM with yourself.")
        return
    client.chat_postMessage(
        channel=config.support_channel_id(),
        thread_ts=msg_ts,
        text=f"<@{helper_id}> is setting up a message thread with <@{target_id}>. Continue?",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<@{helper_id}> is setting up a message thread with <@{target_id}>. Continue?",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Continue"},
                        "style": "primary",
                        "action_id": "dm_setup_continue",
                        "value": json.dumps(
                            {
                                "thread_ts": msg_ts,
                                "ticket_ts": ticket["support_ts"] if ticket else None,
                                "helper_id": helper_id,
                                "target": target_id,
                            }
                        ),
                    },
                    {
                        **_button("Cancel", "dm_setup_cancel"),
                        "value": json.dumps({"target": target_id, "helper_id": helper_id}),
                    },
                ],
            },
        ],
    )


def _dm_setup_guard(client, body, payload: dict) -> bool:
    """Only the helper who started a `?@user` flow (or an admin) may drive it."""
    caller = body["user"]["id"]
    if caller == payload.get("helper_id") or helpers.is_admin(caller):
        return True
    client.chat_postEphemeral(
        channel=body["channel"]["id"],
        thread_ts=body["message"].get("thread_ts"),
        user=caller,
        text=f"Only <@{payload.get('helper_id')}> (who started this) or an admin can do that.",
    )
    return False


@app.action("dm_setup_continue")
def handle_dm_setup_continue(ack, body, client):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    if not helpers.is_helper(body["user"]["id"]) or not _dm_setup_guard(client, body, payload):
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view=home.dm_setup_modal(payload["target"], payload["thread_ts"]),
    )


@app.action("dm_setup_cancel")
def handle_dm_setup_cancel(ack, body, client):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    if not helpers.is_helper(body["user"]["id"]) or not _dm_setup_guard(client, body, payload):
        return
    _replace_prompt(client, body, f"DM setup with <@{payload.get('target')}> cancelled.")


@app.view("dm_setup_submit")
def handle_dm_setup_submit(ack, body, client, view):
    caller_id = body["user"]["id"]
    if not helpers.is_helper(caller_id):
        ack()
        return
    metadata = json.loads(view["private_metadata"])
    values = view["state"]["values"]
    target = values["target"]["value"]["selected_user"]
    message = values["message"]["value"]["value"].strip()
    delay = values["delay"]["value"]["value"]

    if target == caller_id:
        ack(response_action="errors", errors={"target": "You can't set up a DM with yourself."})
        return
    if not message:
        ack(response_action="errors", errors={"message": "Write the message to send."})
        return
    send_at, problem = scheduling.parse_when(delay)
    if problem:
        ack(response_action="errors", errors={"delay": problem})
        return
    support_channel = config.support_channel_id()
    if not support_channel:
        ack(response_action="errors", errors={"target": "Support isn't set up yet — pick a support channel first."})
        return

    # The DM channel is opened now (not at send time) so the conversation —
    # and the thread the message will land in — exists either way.
    try:
        dm_channel = client.conversations_open(users=[target])["channel"]["id"]
    except Exception as e:
        logging.warning("Couldn't open a DM with %s: %s", target, e)
        ack(response_action="errors", errors={"target": "I can't DM that person — are they in this workspace?"})
        return
    ack()

    # The conversation becomes a ticket of its own, so it gets the same
    # machinery as one the user started: their replies in the DM thread relay
    # into its support thread, and helpers answer there with `?…`. The bot's
    # intro is the DM message the thread hangs off.
    when = scheduling.humanize(send_at)
    intro = client.chat_postMessage(
        channel=dm_channel,
        text=f"<@{caller_id}> from {config.event_name()} support will send you a message here {when}. "
        "Reply in this thread to answer.",
    )
    text = (
        f"<@{caller_id}> opened a DM with *{real_name(target)}* (<@{target}>) — reply here with `?…` to message them. "
        f"First message ({when}):\n{_quote(message)}"
    )
    posted = client.chat_postMessage(channel=support_channel, text=text, blocks=_ticket_blocks(text))
    db.create_ticket(posted["ts"], dm_channel, intro["ts"], target, message, support_text=text)
    # The helper spoke first, so it isn't waiting on a reply from support.
    db.touch_helper_activity(posted["ts"])
    db.set_ticket_claim(posted["ts"], caller_id)
    refresh_ticket_message(client, posted["ts"])
    update_stats(client)

    scheduled_id = db.create_scheduled_dm(
        dm_channel, target, caller_id, message, send_at, thread_ts=intro["ts"], support_ts=posted["ts"]
    )
    # The ticket this was started from, if any — a `?@user` posted as its own
    # channel message has none, and the new ticket above stands alone.
    origin = db.get_ticket_by_support_ts(metadata["ticket_ts"]) if metadata.get("ticket_ts") else None
    if origin is not None:
        _log(
            client,
            origin,
            f"<@{caller_id}> started a DM thread with <@{target}> (message #{scheduled_id}, {when}) — "
            f"it has its own ticket in this channel. First message sends at {send_at} UTC.",
        )


def _thread_message(client, channel: str, thread_ts: str, msg_ts: str) -> dict | None:
    """One message from a thread, as it is now (so files/text reflect any
    edits since a prompt about it was posted)."""
    try:
        replies = client.conversations_replies(
            channel=channel, ts=thread_ts, oldest=msg_ts, latest=msg_ts, inclusive=True, limit=5
        )
    except Exception as e:
        logging.warning("Couldn't fetch thread message %s: %s", msg_ts, e)
        return None
    return next((m for m in replies.get("messages", []) if m.get("ts") == msg_ts), None)


@app.action("confirm_user_files")
def handle_confirm_user_files(ack, body, client):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    dm_channel = body["channel"]["id"]
    ticket = db.get_ticket_by_dm(dm_channel, payload["thread_ts"])
    if db.get_user_message(dm_channel, payload["msg_ts"]) is not None:
        _replace_prompt(client, body, "Sent to the support team ✅")  # double-click
        return
    message = _thread_message(client, dm_channel, payload["thread_ts"], payload["msg_ts"])
    if ticket is None or message is None:
        _replace_prompt(client, body, "I couldn't find that message any more — was it deleted? Try sending it again.")
        return
    db.clear_outbox(payload["thread_ts"], payload["msg_ts"])
    files = attachments.relayable(message.get("files"))
    _relay_user_followup(
        client, ticket, body["user"]["id"], message.get("text", ""), files, dm_channel, payload["msg_ts"]
    )
    _replace_prompt(client, body, "Sent to the support team ✅")


@app.action("cancel_user_files")
def handle_cancel_user_files(ack, body, client):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    # Only whoever was asked can answer — anyone else in the thread gets told
    # whose prompt it is instead of silently cancelling it. Prompts from before
    # `asker` was recorded leave it out; those stay cancellable as they were.
    asker = payload.get("asker")
    if asker and body["user"]["id"] != asker and not helpers.is_admin(body["user"]["id"]):
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            thread_ts=body["message"].get("thread_ts"),
            user=body["user"]["id"],
            text="That's someone else's prompt — only they (or an admin) can cancel it.",
        )
        return
    db.clear_outbox(payload["thread_ts"], payload["msg_ts"])
    _replace_prompt(client, body, "No worries — not sent.")


@app.action("confirm_helper_files")
def handle_confirm_helper_files(ack, body, client, respond):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    user_id = body["user"]["id"]
    ticket = db.get_ticket_by_support_ts(payload["thread_ts"])
    if not helpers.is_helper(user_id) or ticket is None:
        respond(replace_original=True, text="That ticket's gone — nothing sent.")
        return
    if db.get_reply_by_support_ts(payload["msg_ts"]) is not None:
        respond(replace_original=True, text="Already sent ✅")
        return
    # Read the message back so only files still on it go — one deleted since
    # the prompt (the wrong screenshot) mustn't reach the user. The stash
    # narrows that to the files the prompt actually showed.
    message = _thread_message(client, config.support_channel_id(), payload["thread_ts"], payload["msg_ts"])
    files = attachments.summary(attachments.relayable(message.get("files"))) if message else []
    stashed = db.get_outbox(payload["thread_ts"], payload["msg_ts"])
    if stashed:
        prompted = {f["id"] for f in stashed["files"]}
        files = [f for f in files if f["id"] in prompted]
    if not files:
        respond(replace_original=True, text="I couldn't find those files any more — were they deleted?")
        return
    db.clear_outbox(payload["thread_ts"], payload["msg_ts"])
    # Only the files go — the text alongside them was posted without `?`,
    # so it's taken as internal chatter.
    _send_helper_reply(client, ticket, user_id, payload["msg_ts"], "", files)
    respond(replace_original=True, text=f"Sent {len(files)} attachment(s) to the user ✅")


@app.action("cancel_helper_files")
def handle_cancel_helper_files(ack, body, respond):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    db.clear_outbox(payload["thread_ts"], payload["msg_ts"])
    respond(delete_original=True)


# --- Edits and deletes ------------------------------------------------------


def _quote_or_empty(text: str) -> str:
    return _quote(text) if text.strip() else "> _(no text)_"


def _helper_reply_text(raw: str) -> str:
    """What a helper's `?` message sends the user: the text after `?`, or a
    macro's canned reply."""
    reply_text = raw[1:].strip()
    macro_text = macros.get_macro(reply_text) if reply_text else None
    return macro_text if macro_text is not None else reply_text


def _deletion_log(who: str, when_sent: str, when_deleted: str, text: str, what: str = "their message") -> str:
    """The audit line for a delete: who, what they'd said, and both times."""
    return f'{who} deleted {what}: "{text.strip() or "(no text)"}", sent at {when_sent}, deleted at {when_deleted}'


def _edit_log(who: str, when_sent: str, when_edited: str, before: str, after: str, what: str = "their message") -> str:
    """The audit line for an edit: who, what it was, when, and what it is now."""
    return (
        f"{who} edited {what}. It originally was sent at {when_sent}, and was originally "
        f'"{before.strip() or "(no text)"}". It was edited at {when_edited} to "{after.strip() or "(no text)"}"'
    )


def _handle_edit_or_delete(event, client, is_dm: bool) -> None:
    """Mirror an edit/delete of a relayed message onto its other copy, and
    log what changed in the ticket's support thread (as Chopsticks), so the
    record survives even when the original doesn't."""
    old = event.get("previous_message") or {}
    new = event.get("message") or {}
    # Deleting a message that has thread replies leaves a "This message was
    # deleted." tombstone, which arrives as an edit rather than a delete.
    deleted = event["subtype"] == "message_deleted" or new.get("subtype") == "tombstone"
    if deleted:
        new = {"ts": new.get("ts")}
    # The bot editing/deleting its own posts (prompts, relayed copies, the
    # stats message) fires these too.
    if old.get("bot_id") or new.get("bot_id"):
        return
    ts = event.get("deleted_ts") or new.get("ts")
    old_text, new_text = old.get("text", ""), new.get("text", "")
    if not ts or (not deleted and old_text == new_text):
        return  # e.g. a link unfurling, not a real edit

    if is_dm:
        _mirror_user_change(client, event["channel"], ts, old, new, deleted)
    else:
        _mirror_helper_change(client, ts, old_text, new_text, deleted, sent_at=_stamp(ts))


def _mirror_user_change(client, dm_channel: str, ts: str, old: dict, new: dict, deleted: bool) -> None:
    sent_at = _stamp(ts)
    now = _now_stamp()

    ticket = db.get_ticket_by_dm(dm_channel, ts)
    if ticket is not None:
        # Their original message: the ticket itself. Re-render it on an edit;
        # a delete keeps it, since the whole thread hangs off it.
        user_id = ticket["user_id"]
        if deleted:
            _log(
                client,
                ticket,
                _deletion_log(_who(user_id), sent_at, now, old.get("text", ""), "their original message"),
            )
            return
        text = _user_message_text(user_id, new["text"], attachments.relayable(new.get("files")))
        db.set_ticket_text(ticket["support_ts"], text, original_text=new["text"])
        refresh_ticket_message(client, ticket["support_ts"])
        _log(
            client,
            ticket,
            _edit_log(_who(user_id), sent_at, now, old.get("text", ""), new["text"], "their original message"),
        )
        return

    followup = db.get_user_message(dm_channel, ts)
    if followup is None:
        return  # not something we relayed
    ticket = db.get_ticket_by_support_ts(followup["support_ts"])
    if ticket is None:
        return
    user_id = followup["user_id"]
    channel = config.support_channel_id()
    try:
        if deleted:
            client.chat_delete(channel=channel, ts=followup["support_copy_ts"])
            db.delete_user_message(dm_channel, ts)
        else:
            client.chat_update(
                channel=channel,
                ts=followup["support_copy_ts"],
                text=_user_message_text(user_id, new["text"], attachments.relayable(new.get("files"))),
            )
    except Exception as e:
        logging.warning("Couldn't mirror user's change to %s: %s", followup["support_copy_ts"], e)

    if deleted:
        _log(client, ticket, _deletion_log(_who(user_id), sent_at, now, old.get("text", "")))
    else:
        _log(client, ticket, _edit_log(_who(user_id), sent_at, now, old.get("text", ""), new["text"]))


def _mirror_helper_change(client, support_ts: str, old_text: str, new_text: str, deleted: bool, sent_at: str) -> None:
    reply = db.get_reply_by_support_ts(support_ts)
    if reply is None or reply["deleted"]:
        return  # internal chatter, or already removed via the shortcut
    ticket = db.get_ticket_by_id(reply["ticket_id"])
    if ticket is None:
        return
    author = reply["author_id"]
    now = _now_stamp()

    # Deleting it, or editing the `?` away (making it internal), unsends it —
    # so the user's copy goes too, matching what the helper did.
    if deleted or (old_text.startswith("?") and not new_text.startswith("?")):
        _unsend_reply(client, reply)
        if deleted:
            _log(client, ticket, _deletion_log(_who(author), sent_at, now, reply["text"], "a reply to the user"))
        else:
            _log(
                client,
                ticket,
                f"{_who(author)} unsent their reply at {now} by removing the `?` — "
                f'it originally was sent at {sent_at}, and was originally "{reply["text"].strip()}"',
            )
        return
    if not new_text.startswith("?"):
        return  # caption of files sent via the "send this?" prompt — only the files went to the user

    reply_text = _render_reply(ticket, author, _helper_reply_text(new_text))
    if reply_text == reply["text"]:
        return
    _edit_relayed_reply(client, reply, reply_text)
    _log(client, ticket, _edit_log(_who(author), sent_at, now, reply["text"], reply_text, "a reply to the user"))


def _unsend_reply(client, reply) -> None:
    try:
        client.chat_delete(channel=reply["dm_channel"], ts=reply["dm_ts"])
    except Exception as e:
        logging.warning("Couldn't delete relayed reply %s: %s", reply["dm_ts"], e)
    db.mark_reply_deleted(reply["support_reply_ts"])


def _edit_relayed_reply(client, reply, new_text: str) -> None:
    client.chat_update(
        channel=reply["dm_channel"],
        ts=reply["dm_ts"],
        text=f"{new_text}\n\n_— <@{reply['author_id']}>, {signature_role(reply['author_id'])}_",
    )
    db.update_reply_text(reply["support_reply_ts"], new_text)


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
    db.create_ticket(posted["ts"], dm_channel, dm_ts, user_id, original_text, support_text=text)
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
    if ticket is None:
        return
    clicker = body["user"]["id"]
    if ticket["claimed_by"] and ticket["claimed_by"] != clicker:
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=clicker,
            text=f"<@{ticket['claimed_by']}> already has this one — only they can let it go.",
        )
        return
    _claim_ticket(client, ticket, clicker)


@app.action("ticket_unclaim")
def handle_ticket_unclaim(ack, body, client):
    ack()
    ticket = _ticket_from_button(client, body)
    if ticket is None:
        return
    clicker = body["user"]["id"]
    if ticket["claimed_by"] and ticket["claimed_by"] != clicker and not helpers.is_admin(clicker):
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=clicker,
            text=f"<@{ticket['claimed_by']}> claimed this one — they (or an admin) can unclaim it.",
        )
        return
    _unclaim_ticket(client, ticket, clicker)


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


@app.action("ticket_reopen")
def handle_ticket_reopen(ack, body, client):
    ack()
    ticket = _ticket_from_button(client, body)
    if ticket is None:
        return
    if ticket["status"] == "open":
        refresh_ticket_message(client, ticket["support_ts"])  # stale buttons — someone already reopened it
        return
    _reopen_ticket(client, ticket, by=body["user"]["id"])


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
    thread_ts = shortcut["message"].get("thread_ts")

    reply = db.get_reply_by_support_ts(support_reply_ts)
    if reply is None:
        client.chat_postEphemeral(
            channel=channel_id, thread_ts=thread_ts, user=caller_id, text="That's not a tracked support reply."
        )
        return
    if reply["deleted"]:
        client.chat_postEphemeral(
            channel=channel_id, thread_ts=thread_ts, user=caller_id, text="That reply was already deleted."
        )
        return
    if not _can_manage_reply(caller_id, reply):
        client.chat_postEphemeral(
            channel=channel_id,
            thread_ts=thread_ts,
            user=caller_id,
            text="Only the sender or an admin can edit this reply.",
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

    ticket = db.get_ticket_by_id(reply["ticket_id"])
    new_text = _render_reply(ticket, reply["author_id"], new_text)
    if new_text == reply["text"]:
        return
    _edit_relayed_reply(client, reply, new_text)
    client.reactions_add(channel=config.support_channel_id(), timestamp=support_reply_ts, name="pencil2")
    _log(
        client,
        ticket,
        _edit_log(
            _who(caller_id), _stamp(support_reply_ts), _now_stamp(), reply["text"], new_text, "a reply to the user"
        ),
    )


@app.shortcut("delete_reply")
def handle_delete_reply_shortcut(ack, shortcut, client):
    ack()
    channel_id = shortcut["channel"]["id"]
    if channel_id != config.support_channel_id():
        return

    support_reply_ts = shortcut["message"]["ts"]
    caller_id = shortcut["user"]["id"]
    thread_ts = shortcut["message"].get("thread_ts")

    reply = db.get_reply_by_support_ts(support_reply_ts)
    if reply is None:
        client.chat_postEphemeral(
            channel=channel_id, thread_ts=thread_ts, user=caller_id, text="That's not a tracked support reply."
        )
        return
    if reply["deleted"]:
        client.chat_postEphemeral(
            channel=channel_id, thread_ts=thread_ts, user=caller_id, text="That reply was already deleted."
        )
        return
    if not _can_manage_reply(caller_id, reply):
        client.chat_postEphemeral(
            channel=channel_id,
            thread_ts=thread_ts,
            user=caller_id,
            text="Only the sender or an admin can delete this reply.",
        )
        return

    _unsend_reply(client, reply)
    _log(
        client,
        db.get_ticket_by_id(reply["ticket_id"]),
        _deletion_log(_who(caller_id), _stamp(support_reply_ts), _now_stamp(), reply["text"], "a reply to the user"),
    )

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
• Drop a file in *without* `?` and I'll ask (just you) whether to send it on
• `?macro-name` — send a macro's canned reply
• `?claim` / `?unclaim` — mark yourself as handling it, or let it go
• `?close` — resolve it without messaging the user
• Edit or delete a `?` reply and the user's copy follows — logged in the thread

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
    as_admin = bool(values["admin"]["admin"].get("selected_options"))
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
    threading.Thread(target=_scheduler_loop, args=(app.client, threading.Event()), daemon=True).start()

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Support bot running (Socket Mode)...")
    handler.start()
