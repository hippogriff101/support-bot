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

    def files_upload_v2(self, **kwargs):
        self.calls.append(("files_upload_v2", kwargs))
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
    client.messages[("C_support", "100.1")] = [{"ts": "200.1", "files": files}]
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


# --- review fixes --------------------------------------------------------------


def _submit_dm_setup(client, delay="now", target="U0TARGET", caller="U0HELPER"):
    view = {
        "private_metadata": json.dumps({"thread_ts": "100.1", "target": target}),
        "state": {
            "values": {
                "target": {"value": {"selected_user": target}},
                "message": {"value": {"value": "Hi {name}"}},
                "delay": {"value": {"value": delay}},
            }
        },
    }
    captured = {}
    app.handle_dm_setup_submit(
        ack=lambda **k: captured.update(k), body={"user": {"id": caller}}, client=client, view=view
    )
    return captured


def test_dm_setup_opens_a_ticket_both_sides_can_use(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    assert "errors" not in _submit_dm_setup(client)
    row = db.list_scheduled_dms("U0HELPER")[0]
    ticket = db.get_ticket_by_support_ts(row["support_ts"])
    assert ticket["user_id"] == "U0TARGET" and ticket["dm_channel"] == "D-U0TARGET"
    assert ticket["dm_ts"] == row["thread_ts"]
    assert ticket["claimed_by"] == "U0HELPER"

    # The first message lands in the intro's thread.
    app.send_due_scheduled_dms(client)
    first = client.first_post("Hi name-U0TARGET")
    assert first["channel"] == "D-U0TARGET" and first["thread_ts"] == ticket["dm_ts"]

    # The recipient's reply in that thread relays like any ticket follow-up…
    app._handle_dm(
        {"channel": "D-U0TARGET", "ts": "5000.1", "thread_ts": ticket["dm_ts"], "user": "U0TARGET", "text": "thanks"},
        client,
    )
    assert db.get_user_message("D-U0TARGET", "5000.1") is not None

    # …and a helper's `?` in the new ticket's thread reaches them.
    app._handle_support_reply(
        {"channel": "C_support", "thread_ts": ticket["support_ts"], "ts": "6000.1", "user": "U0HELPER", "text": "?np"},
        client,
    )
    reply = next(p for p in client.calls_named("chat_postMessage") if p["text"].startswith("np\n"))
    assert reply["channel"] == "D-U0TARGET" and reply["thread_ts"] == ticket["dm_ts"]


def test_dm_setup_rejects_self(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    captured = _submit_dm_setup(client, target="U0HELPER")
    assert captured.get("response_action") == "errors" and "target" in captured["errors"]


def test_failed_scheduled_dm_tells_the_helper(support_env):
    class FailingDM(FakeClient):
        def chat_postMessage(self, **kwargs):
            if kwargs.get("channel", "").startswith("D-") and "username" in kwargs:
                raise RuntimeError("not_in_channel")
            return super().chat_postMessage(**kwargs)

    client = FailingDM()
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    _submit_dm_setup(client)
    app.send_due_scheduled_dms(client)
    note = client.first_post("couldn't be sent")
    assert note is not None and note["channel"] == "C_support"


def test_dm_setup_cancel_is_owner_only(client, support_env):
    helpers.add_helper("U0OTHER", "Support")
    body = {
        "actions": [{"value": json.dumps({"target": "U0TARGET", "helper_id": "U0HELPER"})}],
        "channel": {"id": "C_support"},
        "message": {"ts": "900.1"},
        "user": {"id": "U0OTHER"},
    }
    app.handle_dm_setup_cancel(ack=_noop_ack, body=body, client=client)
    assert client.calls_named("chat_update") == []
    assert client.calls_named("chat_postEphemeral")


def test_helpers_own_ticket_question_mark_goes_to_support(client, support_env):
    db.create_ticket("100.1", "D_helper", "d1", "U0HELPER", "help", support_text="x")
    app._handle_dm(
        {"channel": "D_helper", "ts": "300.1", "thread_ts": "d1", "user": "U0HELPER", "text": "?is this a bug"}, client
    )
    assert db.get_user_message("D_helper", "300.1") is not None
    assert db.get_ticket_by_support_ts("100.1")["status"] == "open"


def test_edit_of_placeholder_reply_keeps_it_filled(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    app._handle_support_reply(
        {"channel": "C_support", "thread_ts": "100.1", "ts": "200.1", "user": "U0HELPER", "text": "?Hi {name} typo"},
        client,
    )
    assert db.get_reply_by_support_ts("200.1")["text"] == "Hi name-U0USER typo"

    app._mirror_helper_change(client, "200.1", "?Hi {name} typo", "?Hi {name} fixed", False, sent_at="x")
    update = client.calls_named("chat_update")[-1]
    assert update["text"].startswith("Hi name-U0USER fixed")


def test_helper_file_deleted_before_confirm_isnt_sent(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    files = [{"id": "F1", "name": "wrong.png", "url_private_download": "https://files/wrong.png", "mode": "hosted"}]
    app._handle_support_reply(
        {"channel": "C_support", "thread_ts": "100.1", "ts": "200.1", "user": "U0HELPER", "text": "", "files": files},
        client,
    )
    client.messages[("C_support", "100.1")] = [{"ts": "200.1", "files": []}]  # file deleted since
    value = client.calls_named("chat_postEphemeral")[0]["blocks"][-1]["elements"][0]["value"]
    body = {"actions": [{"value": value}], "channel": {"id": "C_support"}, "user": {"id": "U0HELPER"}}
    app.handle_confirm_helper_files(ack=_noop_ack, body=body, client=client, respond=lambda **k: None)
    assert db.get_reply_by_support_ts("200.1") is None


def test_helper_file_cancel_clears_stash(client, support_env):
    db.add_outbox("100.1", "200.1", "U0HELPER", [{"id": "F1", "name": "a.png", "url": "u"}])
    body = {"actions": [{"value": json.dumps({"thread_ts": "100.1", "msg_ts": "200.1", "asker": "U0HELPER"})}]}
    app.handle_cancel_helper_files(ack=_noop_ack, body=body, respond=lambda **k: None)
    assert db.get_outbox("100.1", "200.1") is None


def test_old_user_file_prompt_still_cancellable(client, support_env):
    body = {
        "actions": [{"value": json.dumps({"thread_ts": "d1", "msg_ts": "300.1"})}],  # no `asker`
        "channel": {"id": "D_user"},
        "message": {"ts": "400.1"},
        "user": {"id": "U0USER"},
    }
    app.handle_cancel_user_files(ack=_noop_ack, body=body, client=client)
    assert "not sent" in client.calls_named("chat_update")[0]["text"]


def test_admin_can_unclaim(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    db.set_ticket_claim("100.1", "U0HELPER")
    body = {"user": {"id": "U0ADMIN"}, "channel": {"id": "C_support"}, "message": {"ts": "100.1"}}
    app.handle_ticket_unclaim(ack=_noop_ack, body=body, client=client)
    assert db.get_ticket_by_support_ts("100.1")["claimed_by"] is None


def test_helper_modal_submit_saves(client, support_env):
    view = {
        "private_metadata": "{}",
        "state": {
            "values": {
                "user": {"value": {"selected_user": "U0NEW"}},
                "role": {"value": {"value": "Clubs"}},
                "admin": {"admin": {"selected_options": [{"value": "admin"}]}},
            }
        },
    }
    captured = {}
    app.handle_helper_submit(
        ack=lambda **k: captured.update(k), body={"user": {"id": "U0ADMIN"}}, client=client, view=view
    )
    assert "errors" not in captured
    assert helpers.get_role("U0NEW") == "Clubs" and helpers.is_admin("U0NEW")


# --- file prompts land in the thread; `?` + files sends the files -------------

IMAGE = {"id": "F1", "name": "shot.png", "url_private_download": "https://files/shot.png", "mode": "hosted"}


def test_helper_file_prompt_is_in_the_thread(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    app._handle_support_reply(
        {"channel": "C_support", "thread_ts": "100.1", "ts": "200.1", "user": "U0HELPER", "text": "", "files": [IMAGE]},
        client,
    )
    assert client.calls_named("chat_postEphemeral")[0]["thread_ts"] == "100.1"


@pytest.mark.parametrize("text", ["?here you go", "?"])
def test_question_mark_with_image_sends_the_image(client, support_env, monkeypatch, text):
    monkeypatch.setattr(attachments, "_download", lambda url, token: b"png")
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    app._handle_support_reply(
        {
            "channel": "C_support",
            "thread_ts": "100.1",
            "ts": "200.1",
            "user": "U0HELPER",
            "text": text,
            "files": [IMAGE],
        },
        client,
    )
    uploads = client.calls_named("files_upload_v2")
    assert len(uploads) == 1
    assert uploads[0]["channel"] == "D_user" and uploads[0]["thread_ts"] == "d1"
    assert uploads[0]["file_uploads"][0]["filename"] == "shot.png"
    assert client.calls_named("chat_postEphemeral") == []  # no "send this?" prompt for a `?` message
