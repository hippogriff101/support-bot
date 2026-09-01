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

import sqlite3
from contextlib import closing

DB_PATH = "tickets.db"


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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
