import importlib
import sqlite3
from contextlib import closing

import pytest

import config
import db
import helpers
import home
import macros


def _backdate(path, support_ts, **columns):
    """Set timestamp columns to `-N days` relative to now, e.g.
    last_user_at=3 → three days ago."""
    with closing(sqlite3.connect(path)) as conn:
        for column, days in columns.items():
            conn.execute(
                f"UPDATE tickets SET {column} = datetime('now', ?) WHERE support_ts = ?",
                (f"-{int(days * 86400)} seconds", support_ts),
            )
        conn.commit()


def test_unanswered_tracks_user_and_helper_activity(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)
    db.create_ticket("s1", "D1", "d1", "U1", "hi", path=path)
    db.create_ticket("s2", "D2", "d2", "U2", "hi", path=path)

    assert {t["support_ts"] for t in db.list_unanswered(path=path)} == {"s1", "s2"}
    assert db.count_unanswered(path=path) == 2

    # A helper reply answers it...
    _backdate(path, "s1", last_user_at=1)
    db.touch_helper_activity("s1", path=path)
    assert [t["support_ts"] for t in db.list_unanswered(path=path)] == ["s2"]

    # ...until the user follows up again.
    _backdate(path, "s1", last_helper_at=0.5)
    db.touch_user_activity("s1", path=path)
    assert {t["support_ts"] for t in db.list_unanswered(path=path)} == {"s1", "s2"}

    # Closed tickets never count as waiting.
    db.set_ticket_status("s2", "closed", path=path)
    assert [t["support_ts"] for t in db.list_unanswered(path=path)] == ["s1"]


def test_unanswered_min_age_and_ordering(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)
    db.create_ticket("new", "D1", "d1", "U1", "hi", path=path)
    db.create_ticket("old", "D2", "d2", "U2", "hi", path=path)
    db.create_ticket("older", "D3", "d3", "U3", "hi", path=path)
    _backdate(path, "old", last_user_at=2)
    _backdate(path, "older", last_user_at=3)

    assert [t["support_ts"] for t in db.list_unanswered(path=path)] == ["older", "old", "new"]
    assert [t["support_ts"] for t in db.list_unanswered(min_age_days=1, path=path)] == ["older", "old"]
    assert [t["support_ts"] for t in db.list_unanswered(limit=1, path=path)] == ["older"]


def test_due_nudges_wait_a_full_period_between_nudges(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)
    db.create_ticket("s1", "D1", "d1", "U1", "hi", path=path)

    assert db.list_due_nudges(2, path=path) == []  # only just asked

    _backdate(path, "s1", last_user_at=3)
    assert [t["support_ts"] for t in db.list_due_nudges(2, path=path)] == ["s1"]

    db.mark_nudged("s1", path=path)
    assert db.list_due_nudges(2, path=path) == []  # just nudged

    _backdate(path, "s1", nudged_at=2.5)
    assert [t["support_ts"] for t in db.list_due_nudges(2, path=path)] == ["s1"]

    db.touch_helper_activity("s1", path=path)
    assert db.list_due_nudges(2, path=path) == []  # answered


def test_clear_claim(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)
    db.create_ticket("s1", "D1", "d1", "U1", "hi", path=path)
    db.set_ticket_claim("s1", "U_helper", path=path)
    db.clear_ticket_claim("s1", path=path)
    assert db.get_ticket_by_support_ts("s1", path=path)["claimed_by"] is None


def test_migration_backfills_activity_from_existing_replies(tmp_path):
    """An already-deployed DB gets last_user_at/last_helper_at filled in from
    what it already knows, so old answered tickets don't all suddenly show
    up as unanswered."""
    path = str(tmp_path / "legacy.db")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                support_ts TEXT UNIQUE NOT NULL,
                dm_channel TEXT NOT NULL,
                dm_ts TEXT NOT NULL,
                user_id TEXT NOT NULL,
                original_text TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                support_reply_ts TEXT UNIQUE NOT NULL,
                ticket_id INTEGER NOT NULL,
                dm_channel TEXT NOT NULL,
                dm_ts TEXT NOT NULL,
                author_id TEXT NOT NULL,
                text TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO tickets (support_ts, dm_channel, dm_ts, user_id, created_at) "
            "VALUES ('answered', 'D1', 'd1', 'U1', datetime('now', '-1 day')), "
            "('waiting', 'D2', 'd2', 'U2', datetime('now', '-1 day'))"
        )
        conn.execute(
            "INSERT INTO replies (support_reply_ts, ticket_id, dm_channel, dm_ts, author_id, text) "
            "VALUES ('r1', 1, 'D1', 'dr1', 'U_helper', 'hi')"
        )
        conn.commit()

    db.init_db(path)
    assert [t["support_ts"] for t in db.list_unanswered(path=path)] == ["waiting"]


def test_config_prefers_saved_then_env_then_default(tmp_path, monkeypatch):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)
    monkeypatch.delenv("STALE_DAYS", raising=False)

    assert config.get("stale_days", path=path) == "2"
    monkeypatch.setenv("STALE_DAYS", "5")
    assert config.get("stale_days", path=path) == "5"
    config.save("stale_days", "0.5", path=path)
    assert config.get("stale_days", path=path) == "0.5"

    with pytest.raises(KeyError):
        config.save("not_a_setting", "x", path=path)


def test_nudge_identity(monkeypatch):
    values = {"nudge_name": "Chopsticks", "nudge_icon": ":chopsticks:"}
    monkeypatch.setattr(config, "get", lambda key, path=None: values[key])
    assert config.nudge_identity() == {"username": "Chopsticks", "icon_emoji": ":chopsticks:"}

    values["nudge_icon"] = "chopsticks"
    assert config.nudge_identity()["icon_emoji"] == ":chopsticks:"

    values["nudge_icon"] = "https://example.com/c.png"
    assert config.nudge_identity() == {"username": "Chopsticks", "icon_url": "https://example.com/c.png"}


def test_last_admin_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(helpers, "HELPERS_PATH", str(tmp_path / "helpers.json"))
    helpers.add_helper("U_admin", "Lead", admin=True)
    helpers.add_helper("U_helper", "Support")

    assert helpers.is_last_admin("U_admin")
    assert not helpers.is_last_admin("U_helper")

    helpers.set_helper("U_helper", "Support", admin=True)
    assert helpers.admin_count() == 2
    assert not helpers.is_last_admin("U_admin")

    # Unlike add_helper, set_helper can demote.
    helpers.set_helper("U_helper", "Support", admin=False)
    assert not helpers.is_admin("U_helper")


def test_delete_macro(tmp_path, monkeypatch):
    monkeypatch.setattr(macros, "MACROS_PATH", str(tmp_path / "macros.json"))
    macros.add_macro("Review", "We're on it")
    assert macros.delete_macro("?review".lstrip("?"))
    assert macros.get_macro("review") is None
    assert not macros.delete_macro("review")


def test_ago():
    assert home.ago(None) == "?"
    assert home.ago("2000-01-01 00:00:00").endswith("h")
    assert "d" in home.ago("2000-01-01 00:00:00")


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point every store at tmp_path. db's functions bind DB_PATH as a
    default argument at import time, so it's reloaded with the env var set
    (and again afterwards to put the real default back)."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "tickets.db"))
    importlib.reload(db)
    db.init_db()
    monkeypatch.setattr(helpers, "HELPERS_PATH", str(tmp_path / "helpers.json"))
    monkeypatch.setattr(macros, "MACROS_PATH", str(tmp_path / "macros.json"))
    macros.bootstrap_defaults()
    yield
    monkeypatch.delenv("DB_PATH")
    importlib.reload(db)


def test_home_views_build_for_every_audience(isolated_state, monkeypatch):
    monkeypatch.setenv("SUPPORT_CHANNEL_ID", "C123")

    setup = home.build_home_view("U_anyone")
    assert any(e.get("action_id") == "open_setup" for b in setup["blocks"] for e in b.get("elements", []))

    helpers.add_helper("U_admin", "Lead", admin=True)
    helpers.add_helper("U_helper", "Support")
    for i in range(40):  # more than fits — must stay under Slack's 100-block cap
        helpers.add_helper(f"U_extra{i}", "Support")
        macros.add_macro(f"macro{i}", "text " * 50)
    db.create_ticket("1.1", "D1", "d1", "U_user", "please <help> & thanks", path=db.DB_PATH)

    public = home.build_home_view("U_anyone")
    helper_view = home.build_home_view("U_helper")
    admin_view = home.build_home_view("U_admin")

    assert len(public["blocks"]) == 1
    assert len(admin_view["blocks"]) <= 100
    assert len(admin_view["blocks"]) > len(helper_view["blocks"])
    waiting = next(
        b["text"]["text"] for b in helper_view["blocks"] if "Waiting on a reply" in b.get("text", {}).get("text", "")
    )
    assert "https://slack.com/archives/C123/p11" in waiting
    assert "&lt;help&gt; &amp; thanks" in waiting
    for block in admin_view["blocks"]:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= 3000


def test_modals_build(isolated_state):
    helpers.add_helper("U_admin", "Lead", admin=True)
    for modal in (
        home.setup_modal(),
        home.config_modal(),
        home.helper_modal(),
        home.helper_modal("U_admin"),
        home.macro_modal(),
        home.macro_modal("review"),
    ):
        assert len(modal["title"]["text"]) <= 24
        assert modal["blocks"]
    assert home.macro_modal("review")["blocks"][1]["element"]["initial_value"].startswith("Hi!")
    admin_box = home.helper_modal("U_admin")["blocks"][2]["element"]
    assert admin_box["initial_options"] == admin_box["options"]
