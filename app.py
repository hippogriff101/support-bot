import json
import logging
import os
import re

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import db
import helpers

load_dotenv()
logging.basicConfig(level=logging.INFO)

SUPPORT_CHANNEL_ID = os.environ["SUPPORT_CHANNEL_ID"]
EVENT_NAME = os.environ.get("EVENT_NAME", "Event")

MENTION_RE = re.compile(r"^<@([A-Z0-9]+)(?:\|[^>]+)?>$")

app = App(token=os.environ["SLACK_BOT_TOKEN"])
db.init_db()

_name_cache: dict[str, str] = {}


def real_name(user_id: str) -> str:
    """First name if we have one, falling back to display/real name. Cached
    for the life of the process since it won't change mid-event."""
    if user_id not in _name_cache:
        profile = app.client.users_info(user=user_id)["user"]["profile"]
        _name_cache[user_id] = (
            profile.get("first_name") or profile.get("display_name") or profile.get("real_name") or user_id
        )
    return _name_cache[user_id]


@app.event("message")
def handle_message(event, client, logger):
    # Ignore the bot's own messages, edits, joins, etc.
    if event.get("bot_id") or event.get("subtype"):
        return

    channel = event["channel"]
    text = event.get("text", "")
    ts = event["ts"]

    # --- Case 1: a DM from a user -> ask if they want it sent to support ---
    if event.get("channel_type") == "im":
        user_id = event["user"]

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
                            "value": json.dumps(
                                {
                                    "dm_channel": channel,
                                    "dm_ts": ts,
                                    "user_id": user_id,
                                    "text": text,
                                }
                            ),
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
        return

    # --- Case 2: any message in the support channel -> gate on helper status, maybe relay ---
    if channel == SUPPORT_CHANNEL_ID:
        user_id = event["user"]

        if not helpers.is_helper(user_id):
            client.reactions_add(channel=channel, timestamp=ts, name="x")
            client.chat_postEphemeral(
                channel=channel,
                user=user_id,
                text="You don't have permission to post here — ping an admin if you think that's wrong.",
            )
            return

        if not event.get("thread_ts"):
            return  # helper chatting outside a ticket thread — nothing to relay

        if not text.startswith("?"):
            return  # internal chatter between helpers, not meant for the user

        ticket = db.get_ticket_by_support_ts(event["thread_ts"])
        if ticket is None:
            return  # reply on a thread we don't recognise as a ticket

        reply_text = text[1:].strip()

        sent = client.chat_postMessage(
            channel=ticket["dm_channel"],
            thread_ts=ticket["dm_ts"],
            text=f"{reply_text}\n\n_— {real_name(user_id)}, {EVENT_NAME} Support_",
        )
        db.create_reply(ts, ticket["id"], ticket["dm_channel"], sent["ts"], user_id, reply_text)
        client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")


@app.action("confirm_send_support")
def handle_confirm(ack, body, client):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    dm_channel = payload["dm_channel"]
    dm_ts = payload["dm_ts"]
    user_id = payload["user_id"]
    original_text = payload["text"]

    quoted_text = "\n".join(f"> {line}" for line in original_text.splitlines())
    posted = client.chat_postMessage(
        channel=SUPPORT_CHANNEL_ID,
        text=f"*{real_name(user_id)}* (<@{user_id}>):\n{quoted_text}",
    )
    db.create_ticket(posted["ts"], dm_channel, dm_ts, user_id, original_text)

    client.reactions_remove(channel=dm_channel, timestamp=dm_ts, name="eyes")
    client.reactions_add(channel=dm_channel, timestamp=dm_ts, name="white_check_mark")

    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Sent to the support team ✅",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "Sent to the support team ✅"}}],
    )


@app.action("cancel_send_support")
def handle_cancel(ack, body, client):
    ack()
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="No worries — not sent.",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "No worries — not sent."}}],
    )


def _can_manage_reply(user_id: str, reply) -> bool:
    return helpers.is_admin(user_id) or user_id == reply["author_id"]


@app.shortcut("edit_reply")
def handle_edit_reply_shortcut(ack, shortcut, client):
    ack()
    channel_id = shortcut["channel"]["id"]
    if channel_id != SUPPORT_CHANNEL_ID:
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
        text=f"{new_text}\n\n_— {real_name(reply['author_id'])}, {EVENT_NAME} Support_",
    )
    db.update_reply_text(support_reply_ts, new_text)
    client.reactions_add(channel=SUPPORT_CHANNEL_ID, timestamp=support_reply_ts, name="pencil2")


@app.shortcut("delete_reply")
def handle_delete_reply_shortcut(ack, shortcut, client):
    ack()
    channel_id = shortcut["channel"]["id"]
    if channel_id != SUPPORT_CHANNEL_ID:
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


@app.command("/support")
def handle_support_command(ack, respond, command):
    ack()
    caller_id = command["user_id"]
    parts = command.get("text", "").strip().split(maxsplit=2)

    usage = (
        "Usage: `/support promote @user <role>` · `/support demote @user` · "
        "`/support list` · `/support whoami`"
    )
    if not parts:
        respond(usage)
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
            f"• <@{uid}> — {info.get('role', f'{EVENT_NAME} Support')}{' (admin)' if info.get('admin') else ''}"
            for uid, info in roster.items()
        ]
        respond("\n".join(lines))
        return

    if subcommand in ("promote", "demote"):
        if not helpers.is_admin(caller_id):
            respond(f"Only admins can {subcommand} helpers.")
            return
        if len(parts) < 2:
            respond(usage)
            return
        match = MENTION_RE.match(parts[1])
        if not match:
            respond("Couldn't find a user to promote — mention them like `@user`.")
            return
        target_id = match.group(1)

        if subcommand == "promote":
            role = parts[2].strip() if len(parts) > 2 else f"{EVENT_NAME} Support"
            helpers.add_helper(target_id, role)
            respond(f"<@{target_id}> is now a helper: *{role}*.")
        else:
            if helpers.remove_helper(target_id):
                respond(f"<@{target_id}> is no longer a helper.")
            else:
                respond(f"<@{target_id}> wasn't a helper.")
        return

    respond(usage)


@app.command("/announce")
def handle_announce_command(ack, respond, client, command):
    ack()
    caller_id = command["user_id"]
    if not helpers.is_helper(caller_id):
        respond("Only helpers can send announcements.")
        return

    message = command.get("text", "").strip()
    if not message:
        respond("Usage: `/announce <message>` — DMs everyone who's opened a support ticket.")
        return

    user_ids = db.get_all_user_ids()
    if not user_ids:
        respond("No one to announce to yet — no tickets on record.")
        return

    sender_name = real_name(caller_id)
    sent = 0
    for user_id in user_ids:
        try:
            client.chat_postMessage(
                channel=user_id,
                text=f"{message}\n\n_— {sender_name}, {EVENT_NAME} Support_",
            )
            sent += 1
        except Exception:
            logging.exception("Failed to DM %s with announcement", user_id)

    respond(f"Announcement sent to {sent}/{len(user_ids)} people.")


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Support bot running (Socket Mode)...")
    handler.start()
