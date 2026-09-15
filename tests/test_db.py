import sqlite3
from contextlib import closing

import db


def test_ticket_lifecycle(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)

    db.create_ticket("support_ts_1", "D123", "dm_ts_1", "U1", "help me", path=path)

    ticket = db.get_ticket_by_support_ts("support_ts_1", path=path)
    assert ticket is not None
    assert ticket["dm_channel"] == "D123"
    assert ticket["dm_ts"] == "dm_ts_1"
    assert ticket["status"] == "open"

    by_dm = db.get_ticket_by_dm("D123", "dm_ts_1", path=path)
    assert by_dm["support_ts"] == "support_ts_1"
    assert db.get_ticket_by_dm("D123", "no-such-ts", path=path) is None

    assert db.count_tickets_by_status("open", path=path) == 1
    assert db.count_tickets_by_status("closed", path=path) == 0

    db.set_ticket_status("support_ts_1", "closed", path=path)
    assert db.count_tickets_by_status("closed", path=path) == 1
    assert db.count_tickets_by_status("open", path=path) == 0

    db.set_ticket_status("support_ts_1", "open", path=path)
    assert db.count_tickets_by_status("open", path=path) == 1
    assert db.count_tickets_by_status("closed", path=path) == 0


def test_reply_lifecycle(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)
    db.create_ticket("support_ts_1", "D123", "dm_ts_1", "U1", "help me", path=path)
    ticket = db.get_ticket_by_support_ts("support_ts_1", path=path)

    db.create_reply("reply_ts_1", ticket["id"], "D123", "dm_reply_ts_1", "U_helper", "here you go", path=path)
    db.create_reply("reply_ts_2", ticket["id"], "D123", "dm_reply_ts_2", "U_helper", "and this", path=path)

    replies = db.get_replies_for_ticket(ticket["id"], path=path)
    assert [r["text"] for r in replies] == ["here you go", "and this"]

    db.mark_reply_deleted("reply_ts_1", path=path)
    replies = db.get_replies_for_ticket(ticket["id"], path=path)
    assert [r["text"] for r in replies] == ["and this"]

    reply = db.get_reply_by_support_ts("reply_ts_2", path=path)
    assert reply["deleted"] == 0

    db.update_reply_text("reply_ts_2", "updated text", path=path)
    assert db.get_reply_by_support_ts("reply_ts_2", path=path)["text"] == "updated text"

    db.mark_reply_deleted("reply_ts_2", path=path)
    assert db.get_reply_by_support_ts("reply_ts_2", path=path)["deleted"] == 1


def test_summary_lifecycle(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)

    db.create_summary("i can't log in", "Reset the password.", path=path)
    db.create_summary("how do i submit?", "Pointed them at the submit page.", path=path)

    newest_first = db.list_summaries(limit=50, path=path)
    assert [s["question"] for s in newest_first] == ["how do i submit?", "i can't log in"]

    limited = db.list_summaries(limit=1, path=path)
    assert len(limited) == 1
    assert limited[0]["question"] == "how do i submit?"


def test_settings_roundtrip(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)

    assert db.get_setting("anchored_stats_ts", path=path) is None

    db.set_setting("anchored_stats_ts", "123.456", path=path)
    assert db.get_setting("anchored_stats_ts", path=path) == "123.456"

    db.set_setting("anchored_stats_ts", "789.012", path=path)
    assert db.get_setting("anchored_stats_ts", path=path) == "789.012"


def test_init_db_migrates_legacy_db_missing_status_column(tmp_path):
    """A tickets.db created before the status column existed (e.g. an
    already-deployed bot's persistent volume) should get migrated in place
    on the next init_db() call, not break."""
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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

    db.init_db(path)  # should not raise
    db.create_ticket("s1", "D1", "d1", "U1", "hi", path=path)

    ticket = db.get_ticket_by_support_ts("s1", path=path)
    assert ticket["status"] == "open"
