from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


class RecoveryPointerStore:
    """Tiny SQLite sidecar for context-compression recovery/audit pointers.

    It intentionally stores metadata and hashes, not raw message contents. Raw
    transcript recovery remains Hermes' job through `state.db` session lineage.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # timeout + busy_timeout + WAL: the gateway and concurrent cron
        # sessions share this file; the default 5s lock wait caused
        # "database is locked" during engine init and a silent fallback
        # to the built-in compressor.
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS compression_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    old_session_id TEXT,
                    new_session_id TEXT,
                    original_count INTEGER NOT NULL,
                    compressed_count INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    summary TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS recovery_pointers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    source_session_id TEXT,
                    message_index INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    rough_tokens INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES compression_events(id)
                )
                """
            )

    def record_event(
        self,
        *,
        old_session_id: str = "",
        new_session_id: str = "",
        original_count: int,
        compressed_count: int,
        total_tokens: int,
        decisions: list[dict[str, Any]],
    ) -> int:
        summary = self._summarize_decisions(decisions)
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO compression_events
                  (created_at, old_session_id, new_session_id, original_count,
                   compressed_count, total_tokens, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (time.time(), old_session_id, new_session_id, original_count, compressed_count, total_tokens, summary),
            )
            event_id = int(cur.lastrowid)
            con.executemany(
                """
                INSERT INTO recovery_pointers
                  (event_id, source_session_id, message_index, role, rough_tokens, decision, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event_id,
                        old_session_id,
                        int(d.get("index", -1)),
                        str(d.get("role", "")),
                        int(d.get("rough_tokens", 0)),
                        str(d.get("decision", "")),
                        str(d.get("reason", "")),
                    )
                    for d in decisions
                ],
            )
            return event_id

    def latest_events(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT id, created_at, old_session_id, new_session_id,
                       original_count, compressed_count, total_tokens, summary
                FROM compression_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _summarize_decisions(decisions: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for d in decisions:
            key = str(d.get("decision", "unknown"))
            counts[key] = counts.get(key, 0) + 1
        return ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no decisions"
