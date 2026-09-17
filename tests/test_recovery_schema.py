"""Schema v3: additive migration tests.

Locks down non-destructive migration behavior BEFORE changing the store:
- fresh DB creates canonical schema and schema version;
- legacy v1 schema (no lifecycle columns) migrates;
- live-shaped v2 DB (operation_id/status/finalized_at ALTERed on) migrates
  without losing rows or pointers;
- migration is idempotent;
- the watchdog's exact finalized-event SQL works after migration;
- unsupported schema versions fail clearly without destructive rewriting.
"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_context_tuner.recovery import RecoveryPointerStore, SCHEMA_VERSION

WATCHDOG_SQL = """
            SELECT id, created_at, old_session_id, original_count,
                   compressed_count, total_tokens, summary
            FROM compression_events
            WHERE status = 'finalized'
            ORDER BY created_at DESC
            LIMIT ?
"""


def _legacy_v1_db(path) -> None:
    """Create a v1-shaped DB exactly as main@HEAD's _init_schema would."""
    con = sqlite3.connect(str(path))
    con.execute(
        """
        CREATE TABLE compression_events (
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
        CREATE TABLE recovery_pointers (
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
    con.execute(
        "INSERT INTO compression_events (created_at, old_session_id, new_session_id,"
        " original_count, compressed_count, total_tokens, summary)"
        " VALUES (1.0, 'old', 'new', 4, 2, 123, 'keep_raw=1')"
    )
    con.execute(
        "INSERT INTO recovery_pointers (event_id, source_session_id, message_index,"
        " role, rough_tokens, decision, reason) VALUES (1, 'old', 0, 'user', 10, 'keep_raw', 'head')"
    )
    con.commit()
    con.close()


def _live_v2_db(path) -> None:
    """A v2 DB as observed live: v1 tables plus ALTERed-on lifecycle columns.

    Mirrors the real sidecar: operation_id all NULL, status default
    'finalized' backfilled onto legacy rows, finalized_at NULL, and a
    schema_info table holding version 2.
    """
    _legacy_v1_db(path)
    con = sqlite3.connect(str(path))
    con.execute("ALTER TABLE compression_events ADD COLUMN operation_id TEXT")
    con.execute(
        "ALTER TABLE compression_events ADD COLUMN status TEXT NOT NULL DEFAULT 'finalized'"
    )
    con.execute("ALTER TABLE compression_events ADD COLUMN finalized_at REAL")
    con.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
    con.execute("INSERT INTO schema_info VALUES (2)")
    con.commit()
    con.close()


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_creates_canonical_schema_and_version(tmp_path):
    store = RecoveryPointerStore(tmp_path / "fresh.sqlite")
    con = sqlite3.connect(str(store.path))
    cols = _columns(con, "compression_events")
    assert SCHEMA_VERSION == 3
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # lifecycle + trigger metadata + fingerprints + budget fields exist
    for name in (
        "operation_id", "status", "started_at", "finalized_at",
        "conversation_id", "trigger_json", "pre_count", "post_count",
        "pre_tokens", "post_tokens", "threshold_tokens", "tail_budget",
        "protected_head", "source_fingerprint", "result_fingerprint",
        "summary_marker", "checkpoint_required",
    ):
        assert name in cols, f"missing column {name}"
    assert _columns(con, "recovery_pointers") >= {
        "event_id", "source_session_id", "range_start", "range_end",
        "fingerprint", "role", "rough_tokens", "decision", "reason",
    }
    con.close()


def test_legacy_v1_schema_migrates_preserving_rows(tmp_path):
    path = tmp_path / "v1.sqlite"
    _legacy_v1_db(path)
    store = RecoveryPointerStore(path)
    con = sqlite3.connect(str(path))
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert con.execute("SELECT COUNT(*) FROM compression_events").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM recovery_pointers").fetchone()[0] == 1
    # legacy rows keep their history visible as finalized events
    assert con.execute(
        "SELECT status FROM compression_events WHERE id = 1"
    ).fetchone()[0] == "finalized"
    con.close()


def test_live_v2_schema_migrates_without_loss(tmp_path):
    path = tmp_path / "v2.sqlite"
    _live_v2_db(path)
    store = RecoveryPointerStore(path)
    con = sqlite3.connect(str(path))
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert con.execute("SELECT COUNT(*) FROM compression_events").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM recovery_pointers").fetchone()[0] == 1
    # the historical row stays finalized with its original created_at
    row = con.execute(
        "SELECT status, created_at, summary FROM compression_events WHERE id = 1"
    ).fetchone()
    assert row[0] == "finalized" and row[1] == 1.0 and row[2] == "keep_raw=1"
    con.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "idem.sqlite"
    _live_v2_db(path)
    RecoveryPointerStore(path)
    con = sqlite3.connect(str(path))
    before_events = con.execute("SELECT COUNT(*) FROM compression_events").fetchone()[0]
    before_pointers = con.execute("SELECT COUNT(*) FROM recovery_pointers").fetchone()[0]
    before_ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='compression_events'"
    ).fetchone()[0]
    con.close()
    # reopen: second migration must not change data or DDL
    RecoveryPointerStore(path)
    con = sqlite3.connect(str(path))
    assert con.execute("SELECT COUNT(*) FROM compression_events").fetchone()[0] == before_events
    assert con.execute("SELECT COUNT(*) FROM recovery_pointers").fetchone()[0] == before_pointers
    assert con.execute(
        "SELECT sql FROM sqlite_master WHERE name='compression_events'"
    ).fetchone()[0] == before_ddl
    con.close()


def test_watchdog_sql_works_after_migration(tmp_path):
    path = tmp_path / "wd.sqlite"
    _live_v2_db(path)
    store = RecoveryPointerStore(path)
    con = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True, timeout=5.0)
    rows = con.execute(WATCHDOG_SQL, (300,)).fetchall()
    assert len(rows) == 1
    con.close()


def test_unsupported_future_schema_fails_clearly(tmp_path):
    path = tmp_path / "future.sqlite"
    _legacy_v1_db(path)
    con = sqlite3.connect(str(path))
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 10}")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="schema"):
        RecoveryPointerStore(path)
    # and it must not have destructively rewritten anything
    con = sqlite3.connect(str(path))
    assert con.execute("SELECT COUNT(*) FROM compression_events").fetchone()[0] == 1
    con.close()


def test_unique_operation_id_enforced(tmp_path):
    store = RecoveryPointerStore(tmp_path / "uniq.sqlite")
    op1 = store.begin_compression(old_session_id="s1", original_count=2, total_tokens=10)
    op2 = store.begin_compression(old_session_id="s1", original_count=2, total_tokens=10)
    assert op1 != op2
    con = sqlite3.connect(str(store.path))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO compression_events (created_at, original_count,"
            " compressed_count, total_tokens, summary, operation_id, status)"
            " VALUES (1.0, 1, 1, 1, 'x', ?, 'started')",
            (op1,),
        )
    con.close()


def test_wal_and_busy_timeout_active(tmp_path):
    store = RecoveryPointerStore(tmp_path / "wal.sqlite")
    con = store._connect()
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    con.close()
