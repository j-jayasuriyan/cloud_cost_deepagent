"""
Lightweight SQLite store for chat session metadata and message history.
Separate from LangGraph's checkpoint DB — this one is for display purposes.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(__file__).parent / "chat_history.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                thread_id   TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                last_active TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id   TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id)")
        con.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_exists(thread_id: str) -> bool:
    with _connect() as con:
        row = con.execute(
            "SELECT 1 FROM sessions WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    return row is not None


def upsert_session(thread_id: str, title: str):
    """Create session if new; always bump last_active."""
    t = _now()
    with _connect() as con:
        con.execute("""
            INSERT INTO sessions(thread_id, title, created_at, last_active)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET last_active = excluded.last_active
        """, (thread_id, title, t, t))
        con.commit()


def touch_session(thread_id: str):
    with _connect() as con:
        con.execute(
            "UPDATE sessions SET last_active = ? WHERE thread_id = ?",
            (_now(), thread_id)
        )
        con.commit()


def save_message(thread_id: str, role: str, content: str):
    with _connect() as con:
        con.execute(
            "INSERT INTO messages(thread_id, role, content, created_at) VALUES(?,?,?,?)",
            (thread_id, role, content, _now())
        )
        con.commit()


def get_sessions(limit: int = 100):
    with _connect() as con:
        rows = con.execute(
            "SELECT thread_id, title, created_at, last_active "
            "FROM sessions ORDER BY last_active DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_messages(thread_id: str):
    with _connect() as con:
        rows = con.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE thread_id = ? ORDER BY id ASC",
            (thread_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_session(thread_id: str):
    with _connect() as con:
        con.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
        con.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))
        con.commit()


def delete_all_sessions():
    with _connect() as con:
        con.execute("DELETE FROM messages")
        con.execute("DELETE FROM sessions")
        con.commit()


# Initialise tables on import
init_db()
