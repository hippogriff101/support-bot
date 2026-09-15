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

    reply = db.get_reply_by_support_ts("reply_ts_1", path=path)
    assert reply["text"] == "here you go"
    assert reply["deleted"] == 0

    db.update_reply_text("reply_ts_1", "updated text", path=path)
    assert db.get_reply_by_support_ts("reply_ts_1", path=path)["text"] == "updated text"

    db.mark_reply_deleted("reply_ts_1", path=path)
    assert db.get_reply_by_support_ts("reply_ts_1", path=path)["deleted"] == 1


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
    assert ticket["claimed_by"] is None
    assert ticket["resolved_by"] is None


def test_ticket_claim(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)
    db.create_ticket("s1", "D1", "d1", "U1", "hi", path=path)

    assert db.get_ticket_by_support_ts("s1", path=path)["claimed_by"] is None

    db.set_ticket_claim("s1", "U_helper", path=path)
    assert db.get_ticket_by_support_ts("s1", path=path)["claimed_by"] == "U_helper"

    db.set_ticket_claim("s1", "U_other_helper", path=path)
    assert db.get_ticket_by_support_ts("s1", path=path)["claimed_by"] == "U_other_helper"


def test_resolved_by_tracks_and_survives_reopen(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)
    db.create_ticket("s1", "D1", "d1", "U1", "hi", path=path)

    db.set_ticket_status("s1", "closed", resolved_by="U_helper", path=path)
    ticket = db.get_ticket_by_support_ts("s1", path=path)
    assert ticket["status"] == "closed"
    assert ticket["resolved_by"] == "U_helper"
    assert db.count_resolved_by_helper(path=path) == {"U_helper": 1}

    # Reopening (no resolved_by passed) leaves the stale value in place, but
    # it no longer counts since count_resolved_by_helper filters on status.
    db.set_ticket_status("s1", "open", path=path)
    ticket = db.get_ticket_by_support_ts("s1", path=path)
    assert ticket["status"] == "open"
    assert ticket["resolved_by"] == "U_helper"
    assert db.count_resolved_by_helper(path=path) == {}


def test_helper_and_ticket_stats(tmp_path):
    path = str(tmp_path / "tickets.db")
    db.init_db(path)

    db.create_ticket("s1", "D1", "d1", "U1", "hi", path=path)
    db.create_ticket("s2", "D2", "d2", "U2", "hi", path=path)
    db.create_ticket("s3", "D1", "d3", "U1", "hi again", path=path)

    assert db.count_tickets_total(path=path) == 3
    assert db.count_unique_requesters(path=path) == 2  # U1 opened two tickets
    assert db.count_tickets_today(path=path) == 3

    t1 = db.get_ticket_by_support_ts("s1", path=path)
    t2 = db.get_ticket_by_support_ts("s2", path=path)
    db.create_reply("r1", t1["id"], "D1", "dr1", "U_helper_a", "hi", path=path)
    db.create_reply("r2", t1["id"], "D1", "dr2", "U_helper_a", "hi again", path=path)
    db.create_reply("r3", t2["id"], "D2", "dr3", "U_helper_b", "hi", path=path)
    db.create_reply("r4", t2["id"], "D2", "dr4", "U_helper_b", "deleted one", path=path)
    db.mark_reply_deleted("r4", path=path)

    assert db.count_replies_by_helper(path=path) == {"U_helper_a": 2, "U_helper_b": 1}
