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
