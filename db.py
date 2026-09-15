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

A "reply" is one helper message relayed into a user's DM. We track the ts
of both copies so a message shortcut on the helper's "?" message in the
support channel can edit or delete the corresponding DM message later:

    support_reply_ts -> the ts of the helper's "?" message in the support
                         channel (what the shortcut fires on)
    dm_channel/dm_ts  -> the DM copy that was actually posted to the user
    author_id         -> who sent the reply, for permission checks
"""

import os
import sqlite3
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

        # Migrate pre-existing DBs that lack the status column.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
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

        # Summaries of resolved tickets, used for the "similar question" AI
        # suggestion and mirrored into the support canvas for humans to browse.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def create_ticket(
    support_ts: str,
    dm_channel: str,
    dm_ts: str,
    user_id: str,
    original_text: str,
    path: str = DB_PATH,
) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO tickets (support_ts, dm_channel, dm_ts, user_id, original_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (support_ts, dm_channel, dm_ts, user_id, original_text),
        )
        conn.commit()


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


def get_replies_for_ticket(ticket_id: int, path: str = DB_PATH) -> list:
    """All non-deleted replies relayed to a ticket's user, oldest first."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM replies WHERE ticket_id = ? AND deleted = 0 ORDER BY id ASC",
            (ticket_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def create_summary(question: str, summary: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO summaries (question, summary) VALUES (?, ?)", (question, summary))
        conn.commit()


def list_summaries(limit: int = 50, path: str = DB_PATH) -> list:
    """Resolved-ticket summaries, newest first — the AI's memory of similar questions."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT question, summary FROM summaries ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]


def update_reply_text(support_reply_ts: str, text: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE replies SET text = ? WHERE support_reply_ts = ?", (text, support_reply_ts))
        conn.commit()


def mark_reply_deleted(support_reply_ts: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE replies SET deleted = 1 WHERE support_reply_ts = ?", (support_reply_ts,))
        conn.commit()


def set_ticket_status(support_ts: str, status: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE tickets SET status = ? WHERE support_ts = ?", (status, support_ts))
        conn.commit()


def count_tickets_by_status(status: str, path: str = DB_PATH) -> int:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM tickets WHERE status = ?", (status,))
        return cur.fetchone()[0]


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
