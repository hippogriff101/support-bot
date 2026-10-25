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

    def views_update(self, **kwargs):
        self.calls.append(("views_update", kwargs))
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

    def chat_getPermalink(self, channel=None, message_ts=None, **kwargs):
        return {"permalink": f"https://slack.test/{channel}/{message_ts}"}

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
    # Typed as a reply, so the prompt goes in that same ticket thread — Slack
    # has no threads on replies.
    assert post["thread_ts"] == "100.1"
    value = json.loads(post["blocks"][-1]["elements"][0]["value"])
    assert value["thread_ts"] == "100.1" and value["ticket_ts"] == "100.1"
    # Nothing was DMed and nothing was scheduled yet.
    assert db.list_scheduled_dms() == []


def test_mention_target_works_as_a_top_level_channel_message(client, support_env):
    """`?@user` typed as its own message in the support channel — no ticket and
    no thread behind it — still starts the flow."""
    app.handle_message(
        {"channel": "C_support", "channel_type": "channel", "ts": "200.1", "user": "U0HELPER", "text": "?<@U0TARGET>"},
        client,
    )
    post = client.first_post("setting up a message thread")
    assert post is not None, "a top-level ?@user in the channel was ignored"
    assert post["thread_ts"] == "200.1"
    value = json.loads(post["blocks"][-1]["elements"][0]["value"])
    assert value["ticket_ts"] is None
    assert db.list_scheduled_dms() == []


def test_bare_id_target_works_top_level_too(client, support_env):
    """The `@U123` form (no `<…>`) also works as a top-level channel message."""
    app.handle_message(
        {"channel": "C_support", "channel_type": "channel", "ts": "200.1", "user": "U0HELPER", "text": "? @U0TARGET"},
        client,
    )
    assert client.first_post("setting up a message thread") is not None


def test_top_level_user_files_get_no_prompt(client, support_env):
    """Without a ticket thread there's nobody to send files to, so a helper
    dropping one in the channel top-level gets no prompt (and no crash)."""
    files = [{"id": "F1", "name": "a.png", "url_private_download": "https://f/a.png", "mode": "hosted"}]
    app.handle_message(
        {
            "channel": "C_support",
            "channel_type": "channel",
            "ts": "200.1",
            "user": "U0HELPER",
            "text": "",
            "files": files,
        },
        client,
    )
    assert client.calls_named("chat_postEphemeral") == []


def test_top_level_close_gets_a_hint_not_a_crash(client, support_env):
    app.handle_message(
        {"channel": "C_support", "channel_type": "channel", "ts": "200.1", "user": "U0HELPER", "text": "?close"},
        client,
    )
    # No ticket to close — the helper is told so (just them), nothing is posted.
    assert client.calls_named("chat_postMessage") == []
    assert "ticket's thread" in client.calls_named("chat_postEphemeral")[0]["text"]


def _metadata_via_continue(client, prompt, user="U0HELPER"):
    """Click Continue on a `?@user` prompt and return the metadata of the modal
    it really opens — so the tests see exactly what the submit handler will."""
    body = {
        "actions": [{"value": prompt["blocks"][-1]["elements"][0]["value"]}],
        "user": {"id": user},
        "trigger_id": "trig",
        "channel": {"id": "C_support"},
        "message": {"ts": "900.1", "thread_ts": prompt.get("thread_ts")},
    }
    app.handle_dm_setup_continue(ack=_noop_ack, body=body, client=client)
    return client.calls_named("views_open")[-1]["view"]["private_metadata"]


def _submit_view(metadata, message="Hello {name}"):
    return {
        "private_metadata": metadata,
        "state": {
            "values": {
                "target": {"value": {"selected_user": "U0TARGET"}},
                "message": {"value": {"value": message}},
                "delay": {"value": {"value": "2h"}},
            }
        },
    }


def test_mention_setup_from_a_ticket_logs_in_that_ticket(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    app.handle_message(
        {
            "channel": "C_support",
            "channel_type": "channel",
            "thread_ts": "100.1",
            "ts": "200.1",
            "user": "U0HELPER",
            "text": "?<@U0TARGET>",
        },
        client,
    )
    metadata = _metadata_via_continue(client, client.first_post("setting up a message thread"))
    captured = {}
    app.handle_dm_setup_submit(
        ack=lambda **k: captured.update(k),
        body={"user": {"id": "U0HELPER"}},
        client=client,
        view=_submit_view(metadata),
    )
    assert "errors" not in captured
    log = client.first_post("started a DM thread")
    assert log is not None and log["thread_ts"] == "100.1"


def test_mention_setup_from_the_channel_end_to_end(client, support_env):
    """The whole flow with no ticket anywhere: the prompt, then the submit
    opening its own ticket for the new conversation."""
    app.handle_message(
        {"channel": "C_support", "channel_type": "channel", "ts": "200.1", "user": "U0HELPER", "text": "?<@U0TARGET>"},
        client,
    )
    metadata = _metadata_via_continue(client, client.first_post("setting up a message thread"))
    captured = {}
    app.handle_dm_setup_submit(
        ack=lambda **k: captured.update(k),
        body={"user": {"id": "U0HELPER"}},
        client=client,
        view=_submit_view(metadata),
    )
    assert "errors" not in captured
    rows = db.list_scheduled_dms("U0HELPER")
    assert len(rows) == 1
    # Freddie's design: the conversation gets a ticket of its own, opened from
    # the channel message, and the scheduled message lands in the DM thread.
    assert rows[0]["recipient_id"] == "U0TARGET"
    assert rows[0]["support_ts"], "the ?@user conversation got no ticket of its own"
    ticket = db.get_ticket_by_support_ts(rows[0]["support_ts"])
    assert ticket is not None and ticket["user_id"] == "U0TARGET"
    assert ticket["dm_channel"] == "D-U0TARGET"


def test_dm_setup_submit_schedules_and_opens_dm(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="*User*:\n> help")
    view = {
        "private_metadata": json.dumps({"thread_ts": "200.1", "ticket_ts": "100.1", "target": "U0TARGET"}),
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


def _sent_reply(client):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    app._handle_support_reply(
        {"channel": "C_support", "thread_ts": "100.1", "ts": "200.1", "user": "U0HELPER", "text": "?hello"}, client
    )
    client.calls.clear()


def _reply_shortcut(caller):
    return {
        "channel": {"id": "C_support"},
        "message": {"ts": "200.1", "thread_ts": "100.1"},
        "user": {"id": caller},
        "trigger_id": "t1",
    }


def _edit_submit(caller):
    view = {
        "private_metadata": json.dumps({"support_reply_ts": "200.1"}),
        "state": {"values": {"text_block": {"text_input": {"value": "words they never said"}}}},
    }
    return {"ack": _noop_ack, "body": {"user": {"id": caller}}, "view": view}


@pytest.mark.parametrize("caller", ["U0ADMIN", "U0OTHER"])
def test_nobody_but_the_sender_can_edit_a_reply(client, support_env, caller):
    helpers.add_helper("U0OTHER", "Support")
    _sent_reply(client)
    app.handle_edit_reply_shortcut(ack=_noop_ack, shortcut=_reply_shortcut(caller), client=client)
    assert client.calls_named("views_open") == []
    assert "Only the sender" in client.calls_named("chat_postEphemeral")[0]["text"]

    # Even a modal opened some other way can't save over someone else's reply.
    app.handle_edit_reply_submit(client=client, **_edit_submit(caller))
    assert client.calls_named("chat_update") == []
    assert db.get_reply_by_support_ts("200.1")["text"] == "hello"


def test_sender_can_still_edit_their_reply(client, support_env):
    _sent_reply(client)
    app.handle_edit_reply_shortcut(ack=_noop_ack, shortcut=_reply_shortcut("U0HELPER"), client=client)
    assert client.calls_named("views_open")
    app.handle_edit_reply_submit(client=client, **_edit_submit("U0HELPER"))
    assert db.get_reply_by_support_ts("200.1")["text"] == "words they never said"


def test_admin_can_delete_someone_elses_reply(client, support_env):
    _sent_reply(client)
    app.handle_delete_reply_shortcut(ack=_noop_ack, shortcut=_reply_shortcut("U0ADMIN"), client=client)
    assert db.get_reply_by_support_ts("200.1")["deleted"]
    assert client.calls_named("chat_delete")


def test_helper_cant_delete_someone_elses_reply(client, support_env):
    helpers.add_helper("U0OTHER", "Support")
    _sent_reply(client)
    app.handle_delete_reply_shortcut(ack=_noop_ack, shortcut=_reply_shortcut("U0OTHER"), client=client)
    assert not db.get_reply_by_support_ts("200.1")["deleted"]
    assert client.calls_named("chat_delete") == []


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


# --- top-level messages in the support channel --------------------------------


def _channel_message(text, user="U0HELPER", **extra):
    return {"channel": "C_support", "channel_type": "channel", "ts": "200.1", "user": user, "text": text, **extra}


def test_non_helper_top_level_question_is_left_alone(client, support_env):
    app.handle_message(_channel_message("?anyone around", user="U0RANDOM"), client)
    assert client.calls == []  # no :x:, no "you don't have permission"


def test_non_helper_in_a_ticket_thread_is_still_told(client, support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    app.handle_message(_channel_message("?hi", user="U0RANDOM", thread_ts="100.1"), client)
    assert client.calls_named("reactions_add")[0]["name"] == "x"


def test_self_target_top_level_doesnt_crash(client, support_env):
    app.handle_message(_channel_message("?<@U0HELPER>"), client)
    note = client.calls_named("chat_postEphemeral")[0]
    assert "yourself" in note["text"] and note["thread_ts"] == "200.1"
    assert client.calls_named("chat_postMessage") == []


def test_mention_in_a_non_ticket_thread_is_ignored(client, support_env):
    app.handle_message(_channel_message("?<@U0TARGET>", thread_ts="150.1"), client)
    assert client.first_post("setting up a message thread") is None


# --- final-check fixes ----------------------------------------------------------


def test_helper_question_in_a_non_ticket_thread_is_told_it_wasnt_sent(client, support_env):
    """e.g. `?bring your laptop` typed under a top-level `?@user` prompt."""
    app.handle_message(_channel_message("?bring your laptop", thread_ts="200.1", ts="210.1"), client)
    note = client.calls_named("chat_postEphemeral")[0]
    assert "wasn't sent" in note["text"] and note["thread_ts"] == "200.1"
    assert client.calls_named("chat_postMessage") == []


def test_non_helper_in_a_non_ticket_thread_is_still_told(client, support_env):
    app.handle_message(_channel_message("?can I help", user="U0RANDOM", thread_ts="150.1", ts="210.1"), client)
    assert client.calls_named("reactions_add")[0]["name"] == "x"


@pytest.mark.parametrize("text", ["?? is the bot down", "?", "?anyone around"])
def test_top_level_chatter_gets_no_hint(client, support_env, text):
    app.handle_message(_channel_message(text), client)
    assert client.calls == []


def test_submit_closes_off_the_prompt(client, support_env):
    app.handle_message(_channel_message("?<@U0TARGET>"), client)
    metadata = _metadata_via_continue(client, client.first_post("setting up a message thread"))
    app.handle_dm_setup_submit(
        ack=_noop_ack, body={"user": {"id": "U0HELPER"}}, client=client, view=_submit_view(metadata)
    )
    ticket_ts = db.list_scheduled_dms("U0HELPER")[0]["support_ts"]
    closed = [u for u in client.calls_named("chat_update") if u["ts"] == "900.1"]
    assert len(closed) == 1
    assert f"https://slack.test/C_support/{ticket_ts}" in closed[0]["text"]
    assert all(b["type"] != "actions" for b in closed[0]["blocks"])  # no Continue left to click


def test_mention_with_files_says_they_werent_sent(client, support_env):
    files = [{"id": "F1", "name": "a.png", "url_private_download": "https://f/a.png", "mode": "hosted"}]
    app.handle_message(_channel_message("?<@U0TARGET>", files=files), client)
    assert client.first_post("setting up a message thread") is not None
    note = client.calls_named("chat_postEphemeral")[0]
    assert "weren't sent" in note["text"]


# --- guides, training and the Home tab -------------------------------------------

import urllib.request  # noqa: E402

import guides  # noqa: E402
import home  # noqa: E402


@pytest.mark.parametrize("kind", ["helper", "admin"])
def test_guide_pages_fit_slack_limits(isolated_state, kind):
    for page in range(len(guides.GUIDES[kind])):
        modal = home.guide_modal(kind, page)
        assert len(modal["title"]["text"]) <= 24
        assert all(len(b["text"]["text"]) <= 3000 for b in modal["blocks"] if b["type"] == "section")


def _nav(modal):
    return [e["action_id"] for b in modal["blocks"] if b["type"] == "actions" for e in b["elements"]]


def test_guide_navigation_and_finish(client, support_env):
    last = len(guides.HELPER_GUIDE) - 1
    assert _nav(home.guide_modal("helper", 0)) == ["guide_page"]
    assert _nav(home.guide_modal("helper", last)) == ["guide_page", "guide_finish"]
    assert _nav(home.guide_modal("helper", last, trained=True)) == ["guide_page"]  # nothing left to finish

    body = {"user": {"id": "U0HELPER"}, "actions": [{"value": "helper:2"}], "view": {"id": "V1", "hash": "h"}}
    app.handle_guide_page(ack=_noop_ack, body=body, client=client)
    assert "Page 3 of" in json.dumps(client.calls_named("views_update")[0]["view"])

    assert "Start training" in json.dumps(home.build_home_view("U0HELPER"))
    app.handle_guide_finish(ack=_noop_ack, body=body, client=client)
    assert db.is_trained("U0HELPER")
    assert "Start training" not in json.dumps(home.build_home_view("U0HELPER"))


def test_non_helper_cant_finish_training(client, support_env):
    body = {"user": {"id": "U0RANDOM"}, "actions": [{"value": "helper"}], "view": {"id": "V1", "hash": "h"}}
    app.handle_guide_finish(ack=_noop_ack, body=body, client=client)
    assert not db.is_trained("U0RANDOM")


def test_admin_guide_falls_back_to_helper_guide_for_helpers(client, support_env):
    body = {"user": {"id": "U0HELPER"}, "actions": [{"value": "admin"}], "trigger_id": "t"}
    app.handle_open_guide(ack=_noop_ack, body=body, client=client)
    assert client.calls_named("views_open")[0]["view"]["title"]["text"] == guides.TITLES["helper"]


def test_setup_checklist_shows_until_done(support_env):
    assert "Setup checklist" in json.dumps(home.build_home_view("U0ADMIN"))
    config.save("event_name", "Bento")
    for uid in helpers.all_helpers():
        db.mark_trained(uid)
    assert "Setup checklist" not in json.dumps(home.build_home_view("U0ADMIN"))


def test_requester_sees_their_own_requests(support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "my laptop is on fire", support_text="x")
    db.create_ticket("101.1", "D_other", "d2", "U0OTHER", "someone else's", support_text="x")
    view = json.dumps(home.build_home_view("U0USER"))
    assert "my laptop is on fire" in view and "someone else" not in view
    assert "waiting for a helper" in view


def test_helper_sees_their_claims(support_env):
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "claimed one", support_text="x")
    db.set_ticket_claim("100.1", "U0HELPER")
    assert "claimed one" in json.dumps(home.build_home_view("U0HELPER"))


def test_resolved_emoji_is_configurable(client, support_env):
    config.save("resolved_emoji", ":ms-green-tick:")
    db.create_ticket("100.1", "D_user", "d1", "U0USER", "help", support_text="x")
    app._close_ticket(client, db.get_ticket_by_support_ts("100.1"), resolved_by="U0HELPER")
    assert client.calls_named("reactions_add")[0]["name"] == "ms-green-tick"


def test_support_guide_command(client, support_env):
    responses = []
    command = {"user_id": "U0ADMIN", "text": "guide admin", "trigger_id": "t"}
    app.handle_support_command(ack=_noop_ack, respond=responses.append, command=command, client=client)
    assert client.calls_named("views_open")[0]["view"]["title"]["text"] == guides.TITLES["admin"]
    command = {"user_id": "U0RANDOM", "text": "guide", "trigger_id": "t"}
    app.handle_support_command(ack=_noop_ack, respond=responses.append, command=command, client=client)
    assert responses == [guides.HOW_TO_GET_HELP]


def test_health_server_answers():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app.start_health_server(port)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
        assert resp.status == 200 and resp.read() == b"ok"
