"""Operation lifecycle state machine tests (Task 5).

Contract: begin creates exactly one started op; finalize records lineage
exactly once; failed ops cannot become finalized; teardown closes pending ops;
invalid transitions are rejected without partial writes; concurrent writers
serialize or fail with a bounded busy error.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from hermes_context_tuner.recovery import RecoveryPointerStore


def _counts(store: RecoveryPointerStore) -> dict[str, int]:
    return store.status_counts()


def test_begin_creates_exactly_one_started_operation(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    op = store.begin_compression(old_session_id="s-old", original_count=4, total_tokens=100)
    assert isinstance(op, str) and op
    counts = _counts(store)
    assert counts.get("started") == 1
    pending = store.latest_pending()
    assert pending and pending["operation_id"] == op
    assert pending["status"] == "started"


def test_finalize_records_lineage_and_post_metrics(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    op = store.begin_compression(old_session_id="s-old", original_count=4, total_tokens=100)
    changed = store.finalize_compression(
        op,
        new_session_id="s-new",
        compressed_count=2,
        post_tokens=60,
        result_fingerprint="ab" * 32,
        summary_marker="summary://s-new/0",
        checkpoint_required=True,
        checkpoint_reason="semantic content externalized",
    )
    assert changed is True
    events = store.latest_events(1)
    ev = events[0]
    assert ev["status"] == "finalized"
    assert ev["new_session_id"] == "s-new"
    assert ev["compressed_count"] == 2
    assert ev["post_count"] == 2
    assert ev["post_tokens"] == 60
    assert ev["result_fingerprint"] == "ab" * 32
    assert ev["checkpoint_required"] == 1
    assert ev["finalized_at"] is not None
    assert store.latest_pending() is None


def test_repeated_finalization_is_idempotent(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    op = store.begin_compression(old_session_id="s-old", original_count=2, total_tokens=10)
    assert store.finalize_compression(op, new_session_id="n", compressed_count=1) is True
    assert store.finalize_compression(op, new_session_id="n", compressed_count=1) is False
    assert _counts(store).get("finalized") == 1
    assert store.status_counts().get("started", 0) == 0


def test_failed_operation_cannot_become_finalized(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    op = store.begin_compression(old_session_id="s-old", original_count=2, total_tokens=10)
    assert store.fail_compression(op, reason="delegate exploded") is True
    with pytest.raises(ValueError, match="transition"):
        store.finalize_compression(op, new_session_id="n", compressed_count=1)
    ev = store.latest_events(1)[0]
    assert ev["status"] == "failed"


def test_teardown_aborts_pending_operations(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    store.begin_compression(old_session_id="s-old", original_count=2, total_tokens=10)
    store.begin_compression(old_session_id="s-old", original_count=3, total_tokens=20)
    aborted = store.abort_pending()
    assert aborted == 2
    assert store.status_counts().get("aborted") == 2
    assert store.latest_pending() is None
    # sweep again: nothing left to abort
    assert store.abort_pending() == 0


def test_mark_interrupted_records_process_death(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    op = store.begin_compression(old_session_id="s-old", original_count=2, total_tokens=10)
    assert store.mark_interrupted(op, reason="process died mid-compress") is True
    assert store.status_counts().get("interrupted") == 1
    # terminal: no further transitions
    with pytest.raises(ValueError):
        store.fail_compression(op)


def test_duplicate_operation_ids_rejected_at_db_level(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    op = store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)
    con = sqlite3.connect(str(store.path))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO compression_events (created_at, original_count,"
            " compressed_count, total_tokens, summary, operation_id, status)"
            " VALUES (1.0, 1, 1, 1, 'dup', ?, 'started')",
            (op,),
        )
    con.close()


def test_invalid_transition_rejected_without_partial_writes(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    op = store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)
    with pytest.raises(ValueError):
        store.finalize_compression("does-not-exist", new_session_id="n", compressed_count=1)
    # store untouched: still exactly one started operation
    assert store.status_counts().get("started") == 1
    assert store.latest_pending()["operation_id"] == op


def test_failed_can_be_marked_interrupted_but_never_finalized(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    op = store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)
    store.fail_compression(op, reason="boom")
    assert store.mark_interrupted(op, reason="sweep") is True
    with pytest.raises(ValueError):
        store.finalize_compression(op, new_session_id="n", compressed_count=1)


def test_concurrent_writers_serialize_or_report_busy(tmp_path):
    store = RecoveryPointerStore(tmp_path / "conc.sqlite")
    outcomes: dict[int, str] = {}

    def writer(n: int) -> None:
        try:
            op = store.begin_compression(
                old_session_id=f"s-{n}", original_count=1, total_tokens=1
            )
            store.finalize_compression(op, new_session_id=f"n-{n}", compressed_count=1)
            outcomes[n] = "finalized"
        except sqlite3.OperationalError as exc:  # bounded busy is acceptable
            assert "locked" in str(exc) or "busy" in str(exc).lower()
            outcomes[n] = "busy"

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not threads[0].is_alive()
    assert len(outcomes) == 6, "every writer must reach a terminal outcome"

    counts = store.status_counts()
    completed = sum(1 for v in outcomes.values() if v == "finalized")
    busy = sum(1 for v in outcomes.values() if v == "busy")
    # A writer that hit a bounded busy error may still have opened its operation
    # before losing the finalize. Such a row must remain 'started' (visible and
    # non-terminal), never 'finalized' — so finalized rows equal the writers
    # that completed, and any leftover 'started' rows are bounded by the busy
    # count. The store does NOT promise "no partial write state".
    assert counts.get("finalized", 0) == completed
    assert counts.get("started", 0) <= busy


def test_finalize_requires_compressed_count(tmp_path):
    store = RecoveryPointerStore(tmp_path / "lc.sqlite")
    op = store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)
    with pytest.raises(ValueError):
        store.finalize_compression(op, new_session_id="n")
    assert store.status_counts().get("started") == 1
