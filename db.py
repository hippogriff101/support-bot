"""
Tiny persistence layer for support tickets.

A "ticket" is one forwarded message in the support channel. We only need
to remember enough to route a helper's threaded reply back to the right
DM/thread:

    support_ts  -> the ts of the message posted in the support channel
                   (this is also the thread_ts helpers reply under)
    dm_channel  -> the DM channel with the original user
    dm_ts       -> the ts of the user's original message (also the
                   thread_ts we reply under, back in the DM)
    user_id     -> who asked the question
    claimed_by  -> helper who ran `?claim` on it, if any (informational —
                   doesn't gate who else can reply)
    resolved_by -> helper who last ran `?close` on it, if any; only
                   meaningful while status is 'closed'
    last_user_at   -> when the user last said something (ticket creation or
                      a follow-up in their DM thread)
    last_helper_at -> when a helper last relayed a reply to the user. A
                      ticket is "unanswered" while this is older than
                      last_user_at (or unset)
    nudged_at      -> when the nudge persona last pinged the support
                      channel about this ticket going unanswered

A "reply" is one helper message relayed into a user's DM. We track the ts
of both copies so a message shortcut on the helper's "?" message in the
support channel can edit or delete the corresponding DM message later:

    support_reply_ts -> the ts of the helper's "?" message in the support
                         channel (what the shortcut fires on)
    dm_channel/dm_ts  -> the DM copy that was actually posted to the user
    author_id         -> who sent the reply, for permission checks
"""

import json
import os
import sqlite3
import time
from contextlib import closing

DB_PATH = os.environ.get("DB_PATH", "tickets.db")


def init_db(path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
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
        conn.commit()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.commit()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS replies (
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
        conn.commit()

        # Migrate pre-existing DBs that lack columns added later.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
            conn.commit()
        if "claimed_by" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN claimed_by TEXT")
            conn.commit()
        if "resolved_by" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN resolved_by TEXT")
            conn.commit()
        if "last_user_at" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN last_user_at TEXT")
            conn.execute("UPDATE tickets SET last_user_at = created_at")
            conn.commit()
        if "last_helper_at" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN last_helper_at TEXT")
            conn.execute(
                "UPDATE tickets SET last_helper_at = "
                "(SELECT MAX(created_at) FROM replies WHERE replies.ticket_id = tickets.id AND deleted = 0)"
            )
            conn.commit()
        if "nudged_at" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN nudged_at TEXT")
            conn.commit()
        if "support_text" not in cols:
            # Left NULL for older tickets — app.py reads it back from Slack
            # the first time it needs to re-render one.
            conn.execute("ALTER TABLE tickets ADD COLUMN support_text TEXT")
            conn.commit()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dm_channel TEXT NOT NULL,
                dm_ts TEXT NOT NULL,
                ticket_id INTEGER NOT NULL,
                support_copy_ts TEXT NOT NULL,
                UNIQUE (dm_channel, dm_ts)
            )
            """
        )
        conn.commit()

        # "Do you want to send this file?" prompts can wait days before anyone
        # clicks, and a button value caps out at 2000 chars, so the files
        # behind a prompt are stashed here instead of travelled in the button.
        # user_id is who the prompt asked (what "cancel" is checked against).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_ts TEXT NOT NULL,
                msg_ts TEXT NOT NULL,
                user_id TEXT NOT NULL,
                files TEXT NOT NULL,
                UNIQUE (thread_ts, msg_ts)
            )
            """
        )
        conn.commit()

        # Scheduled helper messages: a helper sets a DM up now and has it sent
        # at a chosen time (see scheduling.py).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_dms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dm_channel TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                helper_id TEXT NOT NULL,
                body TEXT NOT NULL,
                send_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                sent_ts TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                thread_ts TEXT NOT NULL DEFAULT '',
                support_ts TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()
        # thread_ts: the DM thread the message lands in. support_ts: the ticket
        # the `?@user` flow opened for it, where failures get reported.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_dms)")}
        for col in ("thread_ts", "support_ts"):
            if col not in cols:
                conn.execute(f"ALTER TABLE scheduled_dms ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                conn.commit()


def create_ticket(
    support_ts: str,
    dm_channel: str,
    dm_ts: str,
    user_id: str,
    original_text: str,
    support_text: str | None = None,
    path: str = DB_PATH,
) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO tickets (support_ts, dm_channel, dm_ts, user_id, original_text, support_text, last_user_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (support_ts, dm_channel, dm_ts, user_id, original_text, support_text),
        )
        conn.commit()


def set_ticket_text(support_ts: str, support_text: str, original_text: str | None = None, path: str = DB_PATH):
    """Store the forwarded message's rendered text (and, after the user
    edits their original DM, the new original text)."""
    with closing(sqlite3.connect(path)) as conn:
        if original_text is None:
            conn.execute("UPDATE tickets SET support_text = ? WHERE support_ts = ?", (support_text, support_ts))
        else:
            conn.execute(
                "UPDATE tickets SET support_text = ?, original_text = ? WHERE support_ts = ?",
                (support_text, original_text, support_ts),
            )
        conn.commit()


def all_tickets(path: str = DB_PATH) -> list:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()


def create_user_message(dm_channel: str, dm_ts: str, ticket_id: int, support_copy_ts: str, path: str = DB_PATH):
    """Remember where a user's follow-up DM was copied to in the support
    thread, so an edit/delete of it can be mirrored there."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_messages (dm_channel, dm_ts, ticket_id, support_copy_ts) VALUES (?, ?, ?, ?)",
            (dm_channel, dm_ts, ticket_id, support_copy_ts),
        )
        conn.commit()


def get_user_message(dm_channel: str, dm_ts: str, path: str = DB_PATH):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT user_messages.*, tickets.support_ts, tickets.user_id FROM user_messages "
            "JOIN tickets ON tickets.id = user_messages.ticket_id "
            "WHERE user_messages.dm_channel = ? AND user_messages.dm_ts = ?",
            (dm_channel, dm_ts),
        ).fetchone()


def delete_user_message(dm_channel: str, dm_ts: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DELETE FROM user_messages WHERE dm_channel = ? AND dm_ts = ?", (dm_channel, dm_ts))
        conn.commit()


def get_ticket_by_id(ticket_id: int, path: str = DB_PATH):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()


def get_ticket_by_support_ts(support_ts: str, path: str = DB_PATH):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM tickets WHERE support_ts = ?", (support_ts,))
        return cur.fetchone()


def create_reply(
    support_reply_ts: str,
    ticket_id: int,
    dm_channel: str,
    dm_ts: str,
    author_id: str,
    text: str,
    path: str = DB_PATH,
) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO replies (support_reply_ts, ticket_id, dm_channel, dm_ts, author_id, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (support_reply_ts, ticket_id, dm_channel, dm_ts, author_id, text),
        )
        conn.commit()


def get_ticket_by_dm(dm_channel: str, dm_ts: str, path: str = DB_PATH):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM tickets WHERE dm_channel = ? AND dm_ts = ?", (dm_channel, dm_ts))
        return cur.fetchone()


def get_reply_by_support_ts(support_reply_ts: str, path: str = DB_PATH):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM replies WHERE support_reply_ts = ?", (support_reply_ts,))
        return cur.fetchone()


def update_reply_text(support_reply_ts: str, text: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE replies SET text = ? WHERE support_reply_ts = ?", (text, support_reply_ts))
        conn.commit()


def mark_reply_deleted(support_reply_ts: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE replies SET deleted = 1 WHERE support_reply_ts = ?", (support_reply_ts,))
        conn.commit()


def set_ticket_status(support_ts: str, status: str, resolved_by: str | None = None, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        if resolved_by is not None:
            conn.execute(
                "UPDATE tickets SET status = ?, resolved_by = ? WHERE support_ts = ?",
                (status, resolved_by, support_ts),
            )
        else:
            conn.execute("UPDATE tickets SET status = ? WHERE support_ts = ?", (status, support_ts))
        conn.commit()


def set_ticket_claim(support_ts: str, user_id: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE tickets SET claimed_by = ? WHERE support_ts = ?", (user_id, support_ts))
        conn.commit()


def clear_ticket_claim(support_ts: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE tickets SET claimed_by = NULL WHERE support_ts = ?", (support_ts,))
        conn.commit()


def touch_user_activity(support_ts: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE tickets SET last_user_at = CURRENT_TIMESTAMP WHERE support_ts = ?", (support_ts,))
        conn.commit()


def touch_helper_activity(support_ts: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE tickets SET last_helper_at = CURRENT_TIMESTAMP WHERE support_ts = ?", (support_ts,))
        conn.commit()


# An open ticket whose user's latest message hasn't had a helper reply since.
_UNANSWERED = "status = 'open' AND (last_helper_at IS NULL OR last_helper_at < COALESCE(last_user_at, created_at))"


def _days_ago(days: float) -> str:
    """A SQLite datetime() modifier for `days` ago — seconds so fractional
    days (e.g. 0.5) work."""
    return f"-{int(days * 86400)} seconds"


def list_unanswered(min_age_days: float = 0, limit: int | None = None, path: str = DB_PATH) -> list:
    """Unanswered tickets, longest-waiting first, optionally only those
    waiting at least `min_age_days`."""
    query = (
        f"SELECT * FROM tickets WHERE {_UNANSWERED} "
        "AND COALESCE(last_user_at, created_at) <= datetime('now', ?) "
        "ORDER BY COALESCE(last_user_at, created_at) ASC"
    )
    params: tuple = (_days_ago(min_age_days),)
    if limit is not None:
        query += " LIMIT ?"
        params += (limit,)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def count_unanswered(path: str = DB_PATH) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM tickets WHERE {_UNANSWERED}").fetchone()[0]


def list_due_nudges(days: float, path: str = DB_PATH) -> list:
    """Tickets unanswered for at least `days` that haven't been nudged in the
    last `days` either — so each stays quiet for a full period between
    nudges instead of re-pinging on every check."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            f"SELECT * FROM tickets WHERE {_UNANSWERED} "
            "AND COALESCE(last_user_at, created_at) <= datetime('now', ?1) "
            "AND (nudged_at IS NULL OR nudged_at <= datetime('now', ?1)) "
            "ORDER BY COALESCE(last_user_at, created_at) ASC",
            (_days_ago(days),),
        ).fetchall()


def mark_nudged(support_ts: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE tickets SET nudged_at = CURRENT_TIMESTAMP WHERE support_ts = ?", (support_ts,))
        conn.commit()


def count_tickets_by_status(status: str, path: str = DB_PATH) -> int:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM tickets WHERE status = ?", (status,))
        return cur.fetchone()[0]


def count_tickets_total(path: str = DB_PATH) -> int:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM tickets")
        return cur.fetchone()[0]


def count_tickets_today(path: str = DB_PATH) -> int:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM tickets WHERE date(created_at) = date('now')")
        return cur.fetchone()[0]


def count_unique_requesters(path: str = DB_PATH) -> int:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT COUNT(DISTINCT user_id) FROM tickets")
        return cur.fetchone()[0]


def count_replies_by_helper(path: str = DB_PATH) -> dict[str, int]:
    """Reply count per helper, keyed by author_id. Excludes deleted replies."""
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT author_id, COUNT(*) FROM replies WHERE deleted = 0 GROUP BY author_id")
        return dict(cur.fetchall())


def count_resolved_by_helper(path: str = DB_PATH) -> dict[str, int]:
    """Currently-resolved ticket count per helper, keyed by resolved_by. A
    reopened ticket's stale resolved_by doesn't count here since the status
    filter excludes it."""
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "SELECT resolved_by, COUNT(*) FROM tickets WHERE status = 'closed' AND resolved_by IS NOT NULL "
            "GROUP BY resolved_by"
        )
        return dict(cur.fetchall())


def get_setting(key: str, path: str = DB_PATH) -> str | None:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None


def set_setting(key: str, value: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def mark_trained(user_id: str, path: str = DB_PATH) -> None:
    """Record that a helper finished the Home tab's helper training. Kept in
    settings rather than helpers.json, which is rewritten on every roster
    edit."""
    set_setting(f"trained:{user_id}", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), path=path)


def is_trained(user_id: str, path: str = DB_PATH) -> bool:
    return get_setting(f"trained:{user_id}", path=path) is not None


def list_claimed_open(user_id: str, limit: int = 10, path: str = DB_PATH) -> list:
    """Open tickets `user_id` has claimed, longest-waiting first."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM tickets WHERE status = 'open' AND claimed_by = ? "
            "ORDER BY COALESCE(last_user_at, created_at) ASC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def list_user_tickets(user_id: str, limit: int = 5, path: str = DB_PATH) -> list:
    """Tickets someone opened (or was messaged about), newest first."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM tickets WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?", (user_id, limit)
        ).fetchall()


# --- "Do you want to send this file?" prompts -------------------------------


def add_outbox(thread_ts: str, msg_ts: str, user_id: str, files: list, path: str = DB_PATH) -> None:
    """Remember the files behind a relay prompt. `files` is the compact
    [{id, name, url}] summary — not the full Slack file objects, which are
    big enough to blow past SQLite's row limits."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO outbox (thread_ts, msg_ts, user_id, files) VALUES (?, ?, ?, ?)",
            (thread_ts, msg_ts, user_id, json.dumps(files)),
        )
        conn.commit()


def get_outbox(thread_ts: str, msg_ts: str, path: str = DB_PATH) -> dict | None:
    """The stashed prompt for a message, or None if there isn't one (never
    asked, or already sent/cancelled — in which case the prompt's buttons
    have been replaced anyway)."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM outbox WHERE thread_ts = ? AND msg_ts = ?", (thread_ts, msg_ts)).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["files"] = json.loads(record["files"])
    return record


def clear_outbox(thread_ts: str, msg_ts: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DELETE FROM outbox WHERE thread_ts = ? AND msg_ts = ?", (thread_ts, msg_ts))
        conn.commit()


# --- Scheduled helper messages (the ?@user flow) ----------------------------


def create_scheduled_dm(
    dm_channel: str,
    recipient_id: str,
    helper_id: str,
    body: str,
    send_at: str,
    thread_ts: str = "",
    support_ts: str = "",
    path: str = DB_PATH,
) -> int:
    """Queue a helper's message to be posted in `dm_channel` (in `thread_ts`'s
    thread, if given) at `send_at` (UTC 'YYYY-MM-DD HH:MM:SS'). Returns the
    new row's id."""
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "INSERT INTO scheduled_dms (dm_channel, recipient_id, helper_id, body, send_at, thread_ts, support_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dm_channel, recipient_id, helper_id, body, send_at, thread_ts, support_ts),
        )
        conn.commit()
        return cur.lastrowid


def get_scheduled_dm(scheduled_id: int, path: str = DB_PATH):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM scheduled_dms WHERE id = ?", (scheduled_id,)).fetchone()


def list_due_scheduled_dms(path: str = DB_PATH) -> list:
    """Pending helper messages whose time has come, oldest deadline first."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM scheduled_dms WHERE status = 'pending' AND send_at <= datetime('now') ORDER BY send_at ASC"
        ).fetchall()


def list_scheduled_dms(helper_id: str | None = None, path: str = DB_PATH) -> list:
    """Pending helper messages — just `helper_id`'s, or everyone's if None."""
    query = "SELECT * FROM scheduled_dms WHERE status = 'pending'"
    params: tuple = ()
    if helper_id is not None:
        query += " AND helper_id = ?"
        params = (helper_id,)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query + " ORDER BY send_at ASC", params).fetchall()


def mark_scheduled_dm_sent(scheduled_id: int, sent_ts: str | None = None, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "UPDATE scheduled_dms SET status = 'sent', sent_ts = ? WHERE id = ?",
            (sent_ts, scheduled_id),
        )
        conn.commit()


def mark_scheduled_dm_failed(scheduled_id: int, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE scheduled_dms SET status = 'failed' WHERE id = ?", (scheduled_id,))
        conn.commit()


def cancel_scheduled_dm(scheduled_id: int, path: str = DB_PATH) -> bool:
    """Drop a pending scheduled message. False if it's already gone/sent."""
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "UPDATE scheduled_dms SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (scheduled_id,)
        )
        conn.commit()
        return cur.rowcount > 0
