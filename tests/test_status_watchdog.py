"""Status/watchdog agreement tests (Task 10).

Status views must distinguish all lifecycle states, agree with the canonical
store, work against a freshly migrated DB with the watchdog's exact SQL, show
a locked store as degraded without breaking compression, and never include
transcript bodies or secrets.
"""
from __future__ import annotations

import json
import sqlite3

from hermes_context_tuner.engine import ContextTunerEngine, context_tuner_command
from hermes_context_tuner.recovery import RecoveryPointerStore

from tests.test_engine_journal import FakeDelegate, make_engine

WATCHDOG_SQL = """
            SELECT id, created_at, old_session_id, original_count,
                   compressed_count, total_tokens, summary
            FROM compression_events
            WHERE status = 'finalized'
            ORDER BY created_at DESC
            LIMIT ?
"""


def test_status_counts_distinguish_all_states(tmp_path):
    store = RecoveryPointerStore(tmp_path / "s.sqlite")
    op1 = store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)
    store.finalize_compression(op1, new_session_id="n", compressed_count=1)
    op2 = store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)
    store.fail_compression(op2, reason="r")
    op3 = store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)
    store.abort_pending(op3)
    op4 = store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)
    store.mark_interrupted(op4, reason="died")
    store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)  # still started

    counts = store.status_counts()
    assert counts == {"finalized": 1, "failed": 1, "aborted": 1, "interrupted": 1, "started": 1}

    # engine-level status surfaces the same view
    engine = make_engine(tmp_path, FakeDelegate())
    engine._store = store
    status = engine.get_status()
    assert status["journal"]["status_counts"] == counts
    assert status["journal"]["pending_operation"] is not None

    # and the JSON tool view carries it too (no bodies, only metadata keys)
    payload = json.loads(engine.handle_tool_call("context_tuner_status", {"limit": 5}))
    assert payload["status"]["journal"]["status_counts"] == counts


def test_watchdog_sql_and_status_agree_on_freshly_migrated_db(tmp_path):
    store = RecoveryPointerStore(tmp_path / "s.sqlite")
    for i in range(3):
        op = store.begin_compression(old_session_id=f"s{i}", original_count=2, total_tokens=100)
        store.finalize_compression(op, new_session_id=f"n{i}", compressed_count=1)
    # one non-finalized op that watchdog must NOT count
    op = store.begin_compression(old_session_id="sx", original_count=1, total_tokens=1)
    store.fail_compression(op, reason="nope")

    con = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True, timeout=5.0)
    wd_rows = con.execute(WATCHDOG_SQL, (300,)).fetchall()
    con.close()
    finalized = store.status_counts().get("finalized", 0)
    assert len(wd_rows) == finalized == 3

    # latest_events (status view source) agrees per-row with watchdog SQL
    events = store.latest_events(10)
    finalized_events = [e for e in events if e["status"] == "finalized"]
    assert len(finalized_events) == len(wd_rows)
    assert {e["id"] for e in finalized_events} == {r[0] for r in wd_rows}


def test_locked_store_shows_degraded_without_breaking_compression(tmp_path):
    class LockedStore:
        """Stand-in for a store stuck behind an exclusive SQLite lock."""

        def status_counts(self):
            raise sqlite3.OperationalError("database is locked")

        def latest_pending(self):
            raise sqlite3.OperationalError("database is locked")

        def begin_compression(self, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    engine = make_engine(tmp_path, FakeDelegate())
    engine._store = LockedStore()
    # journal summary degrades visibly instead of raising
    status = engine.get_status()
    assert status["journal"]["available"] is False
    assert "locked" in status["journal"]["degraded"]

    # compression itself is unaffected: delegate result returns, failure counted
    result = engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    assert len(result) == 1
    assert engine.get_status()["health"]["audit_failures"] >= 1


def test_command_output_never_contains_bodies(tmp_path, monkeypatch):
    secret = "TOTALLY-SECRET-CREDENTIAL-BODY"
    store = RecoveryPointerStore(tmp_path / "s.sqlite")
    op = store.begin_compression(
        old_session_id="s",
        original_count=1,
        total_tokens=5,
        messages=[{"role": "user", "content": secret}],
        decisions=[{"index": 0, "role": "user", "rough_tokens": 5, "decision": "summarize", "reason": "r"}],
    )
    store.finalize_compression(op, new_session_id="n", compressed_count=1)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # context_tuner_command reads default_store_path() -> HERMES_HOME/context_tuner/recovery.sqlite
    import shutil

    target = tmp_path / "context_tuner" / "recovery.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(tmp_path / "s.sqlite"), str(target))
    out = context_tuner_command("")
    assert secret not in out
    assert "[finalized]" in out or "[started]" in out
    assert "journal totals" in out


def test_handle_tool_call_caps_output(tmp_path):
    store = RecoveryPointerStore(tmp_path / "s.sqlite")
    for i in range(50):
        op = store.begin_compression(old_session_id=f"s{i}", original_count=10, total_tokens=10)
        store.finalize_compression(op, new_session_id=f"n{i}", compressed_count=1)
    engine = make_engine(tmp_path, FakeDelegate())
    engine._store = store
    payload = json.loads(engine.handle_tool_call("context_tuner_status", {"limit": 3}))
    assert len(payload["events"]) == 3
    payload_default = json.loads(engine.handle_tool_call("context_tuner_status", {}))
    assert len(payload_default["events"]) == 5
    assert engine.handle_tool_call("not_a_tool", {}) .startswith('{"error"')
