"""Tests for the five helper-facing features added on top of upstream:

1. ticket buttons that follow the ticket's state,
2. "do you want to send this?" file prompts,
3. edit/delete mirroring with an audit log,
4. the `?@user` scheduled-DM flow,
5. Home-tab broadcasts.

Anything that needs the real Slack client isn't tested here — the fake client
covers the calls the code makes, and the pure logic (delay parsing, placeholder
rendering, block building, DB round-trips) is tested directly.
"""

import importlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

import app
import attachments
import config
import db
import helpers
import home
import macros
import scheduling


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point every store at tmp_path — see tests/test_features.py for why db
    has to be reloaded rather than patched."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "tickets.db"))
    importlib.reload(db)
    db.init_db()
    monkeypatch.setattr(helpers, "HELPERS_PATH", str(tmp_path / "helpers.json"))
    monkeypatch.setattr(macros, "MACROS_PATH", str(tmp_path / "macros.json"))
    macros.bootstrap_defaults()
    yield
    monkeypatch.delenv("DB_PATH")
    importlib.reload(db)


# --- 1. ticket buttons follow state -----------------------------------------


def test_ticket_blocks_reflect_state():
    """The buttons on a ticket's message match what can happen to it next."""
    open_ticket = {"status": "open", "claimed_by": None, "resolved_by": None}
    claimed = {"status": "open", "claimed_by": "U_a", "resolved_by": None}
    closed = {"status": "closed", "claimed_by": "U_a", "resolved_by": "U_a"}

    def ids(ticket, viewer=None):
        blocks = app._ticket_blocks("hi", ticket, viewer)
        return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]

    assert ids(open_ticket) == ["ticket_claim", "ticket_resolve"]
    # Whoever claimed it can let it go; anyone else just sees who's on it.
    assert ids(claimed, viewer="U_a") == ["ticket_unclaim", "ticket_resolve"]
    assert ids(claimed, viewer="U_b") == ["ticket_claim", "ticket_resolve"]
    assert ids(closed) == ["ticket_reopen"]


def test_ticket_blocks_show_claim_context_line():
    claimed = {"status": "open", "claimed_by": "U_a", "resolved_by": None}
    blocks = app._ticket_blocks("hi", claimed, viewer="U_b")
    context = next(b for b in blocks if b["type"] == "context")
    assert "Claimed by <@U_a>" in context["elements"][0]["text"]


def test_ticket_blocks_truncate_long_text():
    """Slack rejects a section over 3000 chars, and the full text is still in
    the message's fallback `text`."""
    blocks = app._ticket_blocks("x" * 5000, None, None)
    assert len(blocks[0]["text"]["text"]) <= 3000
    assert blocks[0]["text"]["text"].endswith("…")


# --- 2. file prompts --------------------------------------------------------


def _file(name="a.png", file_id="F1"):
    return {
        "id": file_id,
        "name": name,
        "url_private_download": f"https://files.slack.com/{file_id}",
        "permalink": f"https://ws.slack.com/files/{file_id}",
        "mode": "hosted",
    }


def test_attachments_summary_and_describe():
    summary = attachments.summary([_file()])
    assert summary == [{"id": "F1", "name": "a.png", "url": "https://files.slack.com/F1"}]
    assert attachments.describe([_file()]) == "<https://ws.slack.com/files/F1|a.png>"


def test_attachments_relayable_drops_tombstones():
    tombstone = {**_file(), "mode": "tombstone"}
    assert attachments.relayable([_file(), tombstone]) == [_file()]


def test_outbox_roundtrip(isolated_state):
    db.add_outbox("t1", "m1", "U1", [{"id": "F1", "name": "a.png", "url": "https://x/F1"}])
    row = db.get_outbox("t1", "m1")
    assert row["user_id"] == "U1"
    assert row["files"] == [{"id": "F1", "name": "a.png", "url": "https://x/F1"}]
    db.clear_outbox("t1", "m1")
    assert db.get_outbox("t1", "m1") is None


def test_yes_no_prompt_lists_files():
    blocks = app._yes_no(
        "Send it?",
        "confirm_helper_files",
        "cancel_helper_files",
        {"thread_ts": "t", "msg_ts": "m"},
        files="<https://x/F1|a.png>",
    )
    assert blocks[-1]["type"] == "actions"
    assert blocks[1]["type"] == "context"
    assert "a.png" in blocks[1]["elements"][0]["text"]
    assert [e["action_id"] for e in blocks[-1]["elements"]] == ["confirm_helper_files", "cancel_helper_files"]


# --- 3. edit/delete audit log ----------------------------------------------


def test_deletion_log_matches_requested_wording():
    line = app._deletion_log("*@Sam*", "2026-10-20 10:00 UTC", "2026-10-20 10:05 UTC", "the old text")
    assert line == (
        '*@Sam* deleted their message: "the old text", sent at 2026-10-20 10:00 UTC, deleted at 2026-10-20 10:05 UTC'
    )


def test_edit_log_matches_requested_wording():
    line = app._edit_log("*@Sam*", "2026-10-20 10:00 UTC", "2026-10-20 10:05 UTC", "before", "after")
    assert line == (
        "*@Sam* edited their message. It originally was sent at 2026-10-20 10:00 UTC, and was originally "
        '"before". It was edited at 2026-10-20 10:05 UTC to "after"'
    )


def test_logs_handle_missing_text():
    assert "(no text)" in app._deletion_log("*@Sam*", "?", "?", "   ")
    assert "(no text)" in app._edit_log("*@Sam*", "?", "?", "", "")


def test_stamp_formats_slack_ts():
    # 2026-10-20 14:23:00 UTC, as Slack writes it: "<epoch>.<microseconds>"
    epoch = int(datetime(2026, 10, 20, 14, 23, tzinfo=timezone.utc).timestamp())
    assert app._stamp(f"{epoch}.486449") == "2026-10-20 14:23 UTC"
    assert app._stamp("not-a-ts") == "not-a-ts"
    assert app._stamp(None) == "?"


# --- 4. delay parsing / placeholders (shared by ?@user and broadcasts) ------


def test_parse_when_relative():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    send_at, problem = scheduling.parse_when("2d", now)
    assert problem is None
    assert send_at == "2026-10-22 12:00:00"

    send_at, problem = scheduling.parse_when("90 minutes", now)
    assert problem is None
    assert send_at == "2026-10-20 13:30:00"

    send_at, problem = scheduling.parse_when("1d 6h", now)
    assert problem is None
    assert send_at == "2026-10-21 18:00:00"

    send_at, problem = scheduling.parse_when("now", now)
    assert (send_at, problem) == ("2026-10-20 12:00:00", None)


def test_parse_when_absolute_and_errors():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    send_at, problem = scheduling.parse_when("2026-10-22 09:00", now)
    assert (send_at, problem) == ("2026-10-22 09:00:00", None)

    assert scheduling.parse_when("banana", now)[1] is not None
    assert scheduling.parse_when("2020-01-01 09:00", now)[1] is not None  # past
    _, problem = scheduling.parse_when("10d", now)  # over the 48h cap
    assert problem is not None and "48 hours" in problem


def test_stagger_pushes_later():
    assert scheduling.stagger("2026-10-20 12:00:00", 60) == "2026-10-20 12:01:00"
    assert scheduling.stagger("garbage", 60) == "garbage"


def test_render_placeholders(isolated_state):
    config.save("event_name", "Bento")
    body = "Hi {name}!\n\n{user-at} {user} on {date} at {event}.\n_— {helper}_"
    out = scheduling.render(body, user_id="U9", name="Sam", helper_id="U2")
    assert "Hi Sam!" in out
    assert "<@U9>" in out
    assert "Sam (@U9)" in out
    assert "Bento" in out
    assert "<@U2>" in out
    # Multi-paragraph, emoji and Slack formatting survive untouched.
    assert "Hi Sam!\n\n" in out


def test_render_leaves_unknown_placeholders_alone(isolated_state):
    assert scheduling.render("{nope} and {name}", user_id="", name="") == "{nope} and there"


# --- 4b. `?@user` target parsing --------------------------------------------


@pytest.mark.parametrize(
    "text,target",
    [
        ("<@U123ABC>", "U123ABC"),
        ("<@U123ABC|sam>", "U123ABC"),
        ("U123ABC", "U123ABC"),
        ("@U123ABC", "U123ABC"),
        ("<@U123ABC> ", "U123ABC"),
    ],
)
def test_mention_target_parses(text, target):
    assert app._mention_target(text) == target


@pytest.mark.parametrize("text", ["hello", "sam", "", "W123ABC x"])
def test_mention_target_rejects_non_mentions(text):
    assert app._mention_target(text) is None


def test_dm_setup_modal_builds():
    modal = home.dm_setup_modal("U123", "t1")
    assert modal["callback_id"] == "dm_setup_submit"
    assert len(modal["title"]["text"]) <= 24
    assert json.loads(modal["private_metadata"])["target"] == "U123"
    assert any(b.get("block_id") == "delay" for b in modal["blocks"])


# --- 5. scheduled DMs and broadcasts ----------------------------------------


def test_scheduled_dm_roundtrip(isolated_state):
    sid = db.create_scheduled_dm("D1", "U1", "U_helper", "hello {name}", "2026-10-20 00:00:00")
    assert db.get_scheduled_dm(sid)["status"] == "pending"
    assert [r["id"] for r in db.list_scheduled_dms("U_helper")] == [sid]
    assert db.list_scheduled_dms("U_other") == []
    assert db.cancel_scheduled_dm(sid) is True
    assert db.cancel_scheduled_dm(sid) is False  # already cancelled


def test_due_scheduled_dms_only_returns_past(isolated_state):
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    db.create_scheduled_dm("D1", "U1", "U_h", "now", past)
    db.create_scheduled_dm("D2", "U2", "U_h", "later", future)
    assert [r["recipient_id"] for r in db.list_due_scheduled_dms()] == ["U1"]


def test_broadcast_roundtrip(isolated_state):
    targets = [("U1", "D1", "2026-10-20 00:00:00"), ("U2", "D2", "2026-10-20 00:00:01")]
    bid = db.create_broadcast("U_admin", "hi {name}", targets)
    assert db.get_broadcast(bid)["body"] == "hi {name}"
    rows = db.broadcast_targets(bid)
    assert [r["dm_channel"] for r in rows] == ["D1", "D2"]
    assert db.list_broadcasts()[0]["total"] == 2

    db.mark_broadcast_target(rows[0]["id"], "sent")
    assert db.broadcast_targets(bid)[0]["status"] == "sent"
    db.mark_broadcast_target(rows[1]["id"], "sent")
    assert db.get_broadcast(bid)["status"] == "sent"


def test_due_broadcast_targets_join(isolated_state):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.create_broadcast("U_admin", "hi", [("U1", "D1", past)])
    due = db.list_due_broadcast_targets()
    assert len(due) == 1
    assert due[0]["author_id"] == "U_admin"


def test_broadcast_modals_and_summary(isolated_state):
    assert home.broadcast_compose_modal()["callback_id"] == "broadcast_compose_submit"
    recipients = home.broadcast_recipients_modal()
    assert recipients["callback_id"] == "broadcast_recipients_submit"
    assert any(b.get("block_id") == "user1" for b in recipients["blocks"])
    assert sum(1 for b in recipients["blocks"] if b.get("block_id", "").startswith("user")) == home.MAX_BROADCAST_PICKS

    bid = db.create_broadcast("U_admin", "hi", [("U1", "D1", "2026-10-20 00:00:00")])
    block = home.broadcast_summary(db.get_broadcast(bid), db.broadcast_targets(bid))
    assert "Broadcast #" in block["text"]["text"]


# --- helper roster broadcast flag -------------------------------------------


def test_can_broadcast_follows_roster(isolated_state):
    helpers.add_helper("U_admin", "Lead", admin=True)
    helpers.add_helper("U_plain", "Support")
    helpers.set_helper("U_poster", "Support", admin=False, broadcast=True)
    assert helpers.can_broadcast("U_admin") is True  # admins always can
    assert helpers.can_broadcast("U_poster") is True
    assert helpers.can_broadcast("U_plain") is False


def test_home_view_shows_broadcast_button_for_broadcasters(isolated_state, monkeypatch):
    monkeypatch.setenv("SUPPORT_CHANNEL_ID", "C123")
    helpers.add_helper("U_admin", "Lead", admin=True)
    helpers.set_helper("U_poster", "Support", admin=False, broadcast=True)
    helpers.add_helper("U_plain", "Support")

    def has_broadcast(user_id):
        view = home.build_home_view(user_id)
        return any(
            e.get("action_id") == "open_broadcast"
            for b in view["blocks"]
            for e in (b.get("elements") or [b.get("accessory")] if b.get("accessory") or b.get("elements") else [])
        )

    assert has_broadcast("U_admin") is True
    assert has_broadcast("U_poster") is True
    assert has_broadcast("U_plain") is False


# --- migrations -------------------------------------------------------------


def test_schema_has_new_tables(isolated_state):
    with closing(sqlite3.connect(db.DB_PATH)) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"outbox", "scheduled_dms", "broadcasts", "broadcast_targets"} <= names
