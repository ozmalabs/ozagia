"""
store.py — Session persistence for the Ozma wire protocol.

Tracks per-session state: nonces (for handshake), ack status, and
confirmed capability sets. Two backends:
    MemorySessionStore  — in-memory dict (default for tests, no persistence)
    SQLiteSessionStore  — stdlib sqlite3, default path ~/.ozma/sessions.db

Both implement the SessionStore typing.Protocol; any object with the right
methods works as a drop-in backend.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import runtime_checkable, Protocol


@runtime_checkable
class SessionStore(Protocol):
    """Structural interface for session persistence backends."""

    def is_acked(self, session_id: str) -> bool: ...
    def set_nonce(self, session_id: str, nonce: str) -> None: ...
    def get_nonce(self, session_id: str) -> str | None: ...
    def mark_acked(
        self,
        session_id: str,
        caps: dict[str, str],
        model_id: str,
        level: int,
    ) -> None: ...
    def get_caps(self, session_id: str) -> dict[str, str]: ...
    def touch(self, session_id: str) -> None: ...


# ---------------------------------------------------------------------------
# MemorySessionStore
# ---------------------------------------------------------------------------

class MemorySessionStore:
    """In-memory store. Thread-safe via dict atomicity. No persistence."""

    def __init__(self) -> None:
        self._nonces: dict[str, str] = {}
        self._acked: dict[str, bool] = {}
        self._caps: dict[str, dict[str, str]] = {}

    def is_acked(self, session_id: str) -> bool:
        return self._acked.get(session_id, False)

    def set_nonce(self, session_id: str, nonce: str) -> None:
        self._nonces[session_id] = nonce

    def get_nonce(self, session_id: str) -> str | None:
        return self._nonces.get(session_id)

    def mark_acked(
        self,
        session_id: str,
        caps: dict[str, str],
        model_id: str,
        level: int,
    ) -> None:
        self._acked[session_id] = True
        self._caps[session_id] = dict(caps)
        self._nonces.pop(session_id, None)

    def get_caps(self, session_id: str) -> dict[str, str]:
        return dict(self._caps.get(session_id, {}))

    def touch(self, session_id: str) -> None:
        pass   # no-op for in-memory store


# ---------------------------------------------------------------------------
# SQLiteSessionStore
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path.home() / ".ozma" / "sessions.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    model_id    TEXT NOT NULL DEFAULT '',
    level       INTEGER NOT NULL DEFAULT 1,
    caps_json   TEXT NOT NULL DEFAULT '{}',
    nonce       TEXT,
    acked       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


class SQLiteSessionStore:
    """
    SQLite-backed session store.

    Thread-safe: uses a per-instance lock around every connection operation.
    Uses check_same_thread=False so the connection can be shared between
    threads under the lock.

    Args:
        path: DB file path. Defaults to ~/.ozma/sessions.db.
              Pass ":memory:" for an ephemeral in-process DB.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = str(path) if path is not None else str(_DEFAULT_DB_PATH)
        self._lock = threading.Lock()

        if self._path != ":memory:":
            os.makedirs(os.path.dirname(self._path), exist_ok=True)

        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_CREATE_TABLE)
            self._conn.commit()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _ensure_row(self, session_id: str) -> None:
        """Insert a default row for session_id if none exists."""
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id) VALUES (?)",
            (session_id,),
        )

    # ── Protocol methods ──────────────────────────────────────────────────

    def is_acked(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT acked FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return bool(row["acked"]) if row else False

    def set_nonce(self, session_id: str, nonce: str) -> None:
        with self._lock:
            self._ensure_row(session_id)
            self._conn.execute(
                "UPDATE sessions SET nonce = ?, last_seen = datetime('now')"
                " WHERE session_id = ?",
                (nonce, session_id),
            )
            self._conn.commit()

    def get_nonce(self, session_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT nonce FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row["nonce"] if row else None

    def mark_acked(
        self,
        session_id: str,
        caps: dict[str, str],
        model_id: str,
        level: int,
    ) -> None:
        with self._lock:
            self._ensure_row(session_id)
            self._conn.execute(
                "UPDATE sessions SET acked = 1, nonce = NULL,"
                " caps_json = ?, model_id = ?, level = ?,"
                " last_seen = datetime('now')"
                " WHERE session_id = ?",
                (json.dumps(caps), model_id, level, session_id),
            )
            self._conn.commit()

    def get_caps(self, session_id: str) -> dict[str, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT caps_json FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return {}
            return json.loads(row["caps_json"])

    def touch(self, session_id: str) -> None:
        with self._lock:
            self._ensure_row(session_id)
            self._conn.execute(
                "UPDATE sessions SET last_seen = datetime('now')"
                " WHERE session_id = ?",
                (session_id,),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
