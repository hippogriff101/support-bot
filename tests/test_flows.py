"""End-to-end-ish tests: drive the real handlers with a fake Slack client.

The pure logic is covered in test_new_features.py; these check the wiring —
that a prompt leads to the right relay, and that `?@user` schedules a row
instead of DMing somebody immediately. The fake client records every call, so
the assertions read like "this message landed in that thread".

Bolt's decorators return the function unchanged (the listener is stored on the
App), so the handlers under test are just ordinary callables.
"""

import importlib
import json

import pytest

import app
import attachments
import config
import db
import helpers


class FakeClient:
    """The slice of WebClient the handlers touch, recording every call."""

    def __init__(self):
        self.token = "xoxb-fake"
        self.calls = []
        self.messages = {}
        self._ts = 1000

    def _next_ts(self):
        self._ts += 1
        return f"{self._ts}.000100"

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat_postMessage", kwargs))
        return {"ts": self._next_ts(), "channel": kwargs.get("channel")}

    def chat_postEphemeral(self, **kwargs):
        self.calls.append(("chat_postEphemeral", kwargs))
        return {"ok": True}

    def chat_update(self, **kwargs):
        self.calls.append(("chat_update", kwargs))
        return {"ok": True}

    def chat_delete(self, **kwargs):
        self.calls.append(("chat_delete", kwargs))
        return {"ok": True}

    def views_open(self, **kwargs):
        self.calls.append(("views_open", kwargs))
        return {"ok": True}

    def views_publish(self, **kwargs):
        self.calls.append(("views_publish", kwargs))
        return {"ok": True}

    def users_info(self, user=None, **kwargs):
        return {"user": {"profile": {"first_name": user, "image_192": None}}}

    def reactions_add(self, **kwargs):
        self.calls.append(("reactions_add", kwargs))
        return {"ok": True}

    def reactions_remove(self, **kwargs):
        self.calls.append(("reactions_remove", kwargs))
        return {"ok": True}

    def conversations_open(self, users=None, **kwargs):
        self.calls.append(("conversations_open", {"users": users, **kwargs}))
        return {"channel": {"id": f"D-{users[0]}"}}

    def conversations_replies(self, **kwargs):
        return {"messages": self.messages.get((kwargs["channel"], kwargs["ts"]), [])}

    def calls_named(self, name):
        return [kw for n, kw in self.calls if n == name]

    def first_post(self, contains):
        for n, kw in self.calls:
            if n == "chat_postMessage" and contains in json.dumps(kw):
                return kw
        return None


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture(autouse=True)
def stub_profiles(monkeypatch):
    """real_name()/avatar_url() call users.info through the module-level
    app.client, which would be a live call. Return a fake profile instead."""
    monkeypatch.setattr(app, "_profile", lambda user_id: {"name": f"name-{user_id}", "icon_url": None})


@pytest.fixture
def support_env(isolated_state, monkeypatch):
    monkeypatch.setenv("SUPPORT_CHANNEL_ID", "C_support")
    importlib.reload(config)
    helpers.add_helper("U0HELPER", "Support")
    helpers.set_helper("U0ADMIN", "Lead", admin=True)
    yield "C_support"
    importlib.reload(config)  # drop the monkeypatched env from config's cache


def _noop_ack(**kwargs):
    return None


# --- feature 2: a helper's file prompt -> confirm -> relayed -----------------


def test_helper_file_prompt_then_confirm(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="*User*:\n> help")
    files = [{"id": "F1", "name": "shot.png", "url_private_download": "https://files/shot.png", "mode": "hosted"}]

    # A helper posts a file without `?` — the bot offers (ephemerally) to send it.
    app._handle_support_reply(
        {"channel": "C_support", "thread_ts": "100.1", "ts": "200.1", "user": "U0HELPER", "text": "", "files": files},
        client,
    )
    prompts = client.calls_named("chat_postEphemeral")
    assert len(prompts) == 1
    assert "Do you want to send" in prompts[0]["text"]
    assert "shot.png" in json.dumps(prompts[0]["blocks"])
    assert db.get_outbox("100.1", "200.1")["files"][0]["name"] == "shot.png"

    # Confirm -> files relayed into the user's DM.
    body = {
        "actions": [{"value": prompts[0]["blocks"][-1]["elements"][0]["value"]}],
        "channel": {"id": "C_support"},
        "user": {"id": "U0HELPER"},
    }
    app.handle_confirm_helper_files(ack=_noop_ack, body=body, client=client, respond=lambda **k: None)
    assert db.get_reply_by_support_ts("200.1") is not None  # recorded as a reply
    assert db.get_outbox("100.1", "200.1") is None  # stash cleared


def test_user_file_prompt_then_confirm(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="*User*:\n> help")
    files = [{"id": "F2", "name": "log.txt", "url_private_download": "https://files/log.txt", "mode": "hosted"}]
    client.messages[("D_user", "d1")] = [{"ts": "300.1", "text": "", "files": files}]

    app._handle_dm(
        {
            "channel": "D_user",
            "thread_ts": "d1",
            "ts": "300.1",
            "user": "U0USER",
            "text": "",
            "files": files,
            "channel_type": "im",
        },
        client,
    )
    prompt = client.first_post("Want me to send")
    assert prompt is not None
    assert "log.txt" in json.dumps(prompt["blocks"])

    body = {
        "actions": [{"value": prompt["blocks"][-1]["elements"][0]["value"]}],
        "channel": {"id": "D_user"},
        "message": {"ts": "400.1"},
        "user": {"id": "U0USER"},
    }
    app.handle_confirm_user_files(ack=_noop_ack, body=body, client=client)
    assert db.get_user_message("D_user", "300.1") is not None


# --- feature 4: ?@user schedules, it doesn't DM immediately ------------------


def test_mention_target_flow_posts_continue(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="*User*:\n> help")
    app._handle_support_reply(
        {"channel": "C_support", "thread_ts": "100.1", "ts": "200.1", "user": "U0HELPER", "text": "?<@U0TARGET>"},
        client,
    )
    post = client.first_post("setting up a message thread")
    assert post is not None
    assert "<@U0HELPER>" in post["text"] and "<@U0TARGET>" in post["text"]
    assert any(e["action_id"] == "dm_setup_continue" for e in post["blocks"][-1]["elements"])
    # Nothing was DMed and nothing was scheduled yet.
    assert db.list_scheduled_dms() == []


def test_dm_setup_submit_schedules_and_opens_dm(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="*User*:\n> help")
    view = {
        "private_metadata": json.dumps({"thread_ts": "100.1", "target": "U0TARGET"}),
        "state": {
            "values": {
                "target": {"value": {"selected_user": "U0TARGET"}},
                "message": {"value": {"value": "Hi {name}, about your ticket"}},
                "delay": {"value": {"value": "2h"}},
            }
        },
    }
    captured = {}
    app.handle_dm_setup_submit(
        ack=lambda **k: captured.update(k), body={"user": {"id": "U0HELPER"}}, client=client, view=view
    )
    assert "errors" not in captured
    rows = db.list_scheduled_dms("U0HELPER")
    assert len(rows) == 1
    assert rows[0]["recipient_id"] == "U0TARGET"
    assert rows[0]["body"] == "Hi {name}, about your ticket"
    # The DM channel was opened up-front (users=[…], never user=).
    opens = client.calls_named("conversations_open")
    assert opens and opens[0]["users"] == ["U0TARGET"]


def test_dm_setup_submit_rejects_bad_delay(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    view = {
        "private_metadata": json.dumps({"thread_ts": "100.1", "target": "U0TARGET"}),
        "state": {
            "values": {
                "target": {"value": {"selected_user": "U0TARGET"}},
                "message": {"value": {"value": "hi"}},
                "delay": {"value": {"value": "10 years"}},
            }
        },
    }
    captured = {}
    app.handle_dm_setup_submit(
        ack=lambda **k: captured.update(k), body={"user": {"id": "U0HELPER"}}, client=client, view=view
    )
    assert captured.get("response_action") == "errors"
    assert db.list_scheduled_dms("U0HELPER") == []


def test_due_scheduled_dm_sends_rendered(client, support_env):
    db.create_scheduled_dm("D-U0TARGET", "U0TARGET", "U0HELPER", "Hi {name}!", "2020-01-01 00:00:00")
    app.send_due_scheduled_dms(client)
    post = client.first_post("Hi ")
    assert post is not None
    assert post["channel"] == "D-U0TARGET"
    assert db.list_scheduled_dms("U0HELPER") == []  # no longer pending


# --- prompt values stay inside a button's 2000-char cap ---------------------


def test_attachments_summary_is_button_safe():
    """A prompt value has to survive json.dumps into a 2000-char button, so
    the stash is three short strings per file, not the whole Slack object."""
    files = [
        {
            "id": "F1",
            "name": "a.png",
            "url_private_download": "https://files.slack.com/F1",
            "thumb_360": "x" * 4000,
            "permalink": "https://ws.slack.com/files/F1",
            "preview": "y" * 4000,
        }
    ]
    value = {"thread_ts": "t", "msg_ts": "m", "asker": "U0ONE", "files": attachments.summary(files)}
    assert len(json.dumps(value)) < 2000
