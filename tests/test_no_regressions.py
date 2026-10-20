"""Regression guard: features 1-3 must keep working after the rewrite.

Upstream (`a6173a4`) already shipped a first version of the live buttons, the
file prompts and the edit/delete mirroring. This branch reworks all three, so
these tests pin down the behaviour that *existed before* — the action_ids an
already-posted message carries, the mirror calls themselves — independently of
the newer wording/UI. If one of these fails, the PR has broken something that
already worked.
"""

import importlib
import json

import pytest

import app
import db
import helpers


@pytest.fixture
def support_env(isolated_state, monkeypatch):
    monkeypatch.setenv("SUPPORT_CHANNEL_ID", "C_support")
    importlib.reload(__import__("config"))
    helpers.add_helper("U0HELPER", "Support")
    yield "C_support"
    importlib.reload(__import__("config"))


class FakeClient:
    """Just enough client for the edit/delete + mirroring paths."""

    def __init__(self):
        self.token = "xoxb-fake"
        self.calls = []
        self.messages = {}
        self._ts = 2000

    def _next_ts(self):
        self._ts += 1
        return f"{self._ts}.000100"

    def chat_postMessage(self, **kw):
        self.calls.append(("chat_postMessage", kw))
        return {"ts": self._next_ts(), "channel": kw.get("channel")}

    def chat_update(self, **kw):
        self.calls.append(("chat_update", kw))
        return {"ok": True}

    def chat_delete(self, **kw):
        self.calls.append(("chat_delete", kw))
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.calls.append(("chat_postEphemeral", kw))
        return {"ok": True}

    def conversations_replies(self, **kw):
        return {"messages": self.messages.get((kw["channel"], kw["ts"]), [])}

    def conversations_history(self, **kw):
        # _fetch_dm_message asks with `latest=<ts>&inclusive=True`.
        key = (kw["channel"], kw.get("latest") or kw.get("ts"))
        return {"messages": self.messages.get(key, [])}

    def reactions_add(self, **kw):
        self.calls.append(("reactions_add", kw))
        return {"ok": True}

    def reactions_remove(self, **kw):
        self.calls.append(("reactions_remove", kw))
        return {"ok": True}

    def conversations_open(self, users=None, **kw):
        self.calls.append(("conversations_open", {"users": users, **kw}))
        return {"channel": {"id": f"D-{users[0]}"}}

    def views_publish(self, **kw):
        self.calls.append(("views_publish", kw))
        return {"ok": True}

    def views_open(self, **kw):
        self.calls.append(("views_open", kw))
        return {"ok": True}

    def calls_named(self, name):
        return [kw for n, kw in self.calls if n == name]


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture(autouse=True)
def stub_profiles(monkeypatch):
    monkeypatch.setattr(app, "_profile", lambda user_id: {"name": f"name-{user_id}", "icon_url": None})


# --- feature 1: the action_ids upstream already put on live messages ---------


def test_upstream_action_ids_still_present():
    """Upstream used exactly these three; a message posted before the upgrade
    still carries them, so they must still be handled."""
    ids = app._ticket_button_ids({"status": "open", "claimed_by": None, "resolved_by": None})
    assert "ticket_claim" in ids
    assert "ticket_resolve" in ids
    assert "ticket_reopen" in app._ticket_button_ids({"status": "closed", "claimed_by": None, "resolved_by": "U1"})


def test_upstream_claim_resolve_reopen_semantics_unchanged():
    """The states upstream distinguished still resolve the same way."""
    open_ = app._ticket_blocks("t", {"status": "open", "claimed_by": None, "resolved_by": None})
    claimed = app._ticket_blocks("t", {"status": "open", "claimed_by": "U_a", "resolved_by": None})
    closed = app._ticket_blocks("t", {"status": "closed", "claimed_by": "U_a", "resolved_by": "U_a"})

    def ids(blocks):
        return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]

    # Upstream: open=Claim+Resolve, claimed=Resolve(+line), closed=Reopen.
    assert "ticket_claim" in ids(open_) and "ticket_resolve" in ids(open_)
    assert "ticket_resolve" in ids(claimed)
    assert ids(closed) == ["ticket_reopen"]
    # The "Claimed by"/"Resolved by" context lines are still there.
    assert any("Claimed by <@U_a>" in e["text"] for b in claimed if b["type"] == "context" for e in b["elements"])
    assert any("Resolved by <@U_a>" in e["text"] for b in closed if b["type"] == "context" for e in b["elements"])


def test_ticket_text_still_truncated_for_slack():
    blocks = app._ticket_blocks("x" * 5000, None)
    assert len(blocks[0]["text"]["text"]) <= 3000


# --- feature 2: the prompts upstream already had, and their action_ids ------


def test_upstream_prompt_action_ids_still_registered():
    src = open("app.py").read()
    # `_yes_no` prompts (helper files / user follow-up files).
    for action in ("confirm_user_files", "cancel_user_files", "confirm_helper_files", "cancel_helper_files"):
        assert f'@app.action("{action}")' in src, action
    # The user's *first* message keeps its own separate prompt + handlers.
    for action in ("confirm_send_support", "cancel_send_support"):
        assert f'@app.action("{action}")' in src, action


def test_yes_no_prompt_shape_unchanged():
    """Upstream's `_yes_no` built section + optional context + actions."""
    blocks = app._yes_no(
        "Send it?",
        "confirm_helper_files",
        "cancel_helper_files",
        {"thread_ts": "t", "msg_ts": "m"},
        files="<https://x/F1|a.png>",
    )
    assert blocks[0]["type"] == "section"
    assert blocks[-1]["type"] == "actions"
    assert [e["action_id"] for e in blocks[-1]["elements"]] == ["confirm_helper_files", "cancel_helper_files"]


def test_first_message_prompt_still_re_fetches_on_confirm(client, support_env):
    """The user's first-message prompt stores only ids and re-reads the message
    on confirm (upstream's approach, still used)."""
    files = [{"id": "F1", "name": "a.png", "url_private_download": "https://f/a.png", "mode": "hosted"}]
    client.messages[("D_u", "d1")] = [{"ts": "d1", "text": "help", "files": files}]

    body = {
        "actions": [{"value": '{"dm_channel": "D_u", "dm_ts": "d1", "user_id": "U0USER"}'}],
        "channel": {"id": "D_u"},
        "message": {"ts": "900.1"},
        "user": {"id": "U0USER"},
    }
    app.handle_confirm(ack=lambda **k: None, body=body, client=client)
    # The ticket was opened in the support channel and the DM marked ✅.
    assert client.calls_named("chat_postMessage"), "confirm_send_support stopped relaying"
    assert db.get_ticket_by_dm("D_u", "d1") is not None
    assert client.calls_named("reactions_add"), "the DM is no longer marked done on confirm"


def test_confirm_is_idempotent_against_a_double_click(client, support_env):
    """Upstream's guard: a second click must not open a second ticket."""
    client.messages[("D_u", "d1")] = [{"ts": "d1", "text": "help"}]
    db.create_ticket("already", "D_u", "d1", "U0USER", "help", support_text="x")
    body = {
        "actions": [{"value": '{"dm_channel": "D_u", "dm_ts": "d1", "user_id": "U0USER"}'}],
        "channel": {"id": "D_u"},
        "message": {"ts": "900.1"},
        "user": {"id": "U0USER"},
    }
    app.handle_confirm(ack=lambda **k: None, body=body, client=client)
    assert client.calls_named("chat_postMessage") == [], "a double-click opened a second ticket"


# --- feature 3: the mirroring calls themselves (separate from log wording) --


def _ticket_with_reply():
    db.create_ticket("100.1", "D_u", "d1", "U0USER", "help", support_text="*U*:\n> help")
    ticket = db.get_ticket_by_support_ts("100.1")
    db.create_reply("200.1", ticket["id"], "D_u", "d-reply-1", "U0HELPER", "original reply")
    return ticket


def test_helper_delete_still_deletes_the_user_copy(client, support_env):
    """Upstream: deleting a helper's reply deletes their DM copy."""
    _ticket_with_reply()
    app._mirror_helper_change(client, "200.1", "original reply", "", True, sent_at="2026-10-20 10:00 UTC")
    deletes = client.calls_named("chat_delete")
    assert deletes, "helper delete no longer removes the user's copy"
    assert deletes[0]["ts"] == "d-reply-1"


def test_helper_edit_still_updates_the_user_copy(client, support_env):
    _ticket_with_reply()
    # An edit has to keep the `?` prefix or it's an "unsend" (the `?` was
    # edited away), which is the upstream behaviour too.
    app._mirror_helper_change(client, "200.1", "?original reply", "?an edit", False, sent_at="2026-10-20 10:00 UTC")
    updates = client.calls_named("chat_update")
    assert updates, "helper edit no longer updates the user's copy"
    assert any("an edit" in json.dumps(u) for u in updates)


def test_user_followup_delete_still_deletes_the_support_copy(client, support_env):
    """Upstream: deleting a user's follow-up deletes its copy in the thread."""
    ticket = _ticket_with_reply()
    db.create_user_message("D_u", "u-msg-1", ticket["id"], "support-copy-1")
    app._mirror_user_change(
        client,
        "D_u",
        "u-msg-1",
        {"text": "hello"},
        {"ts": "u-msg-1"},
        deleted=True,
    )
    deletes = client.calls_named("chat_delete")
    assert any(d["ts"] == "support-copy-1" for d in deletes), "user follow-up delete no longer removes the copy"


def test_user_followup_edit_still_updates_the_support_copy(client, support_env):
    ticket = _ticket_with_reply()
    db.create_user_message("D_u", "u-msg-2", ticket["id"], "support-copy-2")
    app._mirror_user_change(
        client,
        "D_u",
        "u-msg-2",
        {"text": "before"},
        {"ts": "u-msg-2", "text": "after"},
        deleted=False,
    )
    updates = client.calls_named("chat_update")
    assert any(u["ts"] == "support-copy-2" for u in updates), "user follow-up edit no longer updates the copy"


def test_bot_messages_and_unfurling_still_ignored(client, support_env):
    """Upstream skipped the bot's own posts and non-edits; that must hold or
    the bot would mirror its own prompts back at itself."""
    _ticket_with_reply()
    before = len(client.calls)
    app._handle_edit_or_delete(
        {"subtype": "message_changed", "channel": "D_u", "message": {"ts": "x", "bot_id": "B1", "text": "hi"}},
        client,
        True,
    )
    assert len(client.calls) == before
