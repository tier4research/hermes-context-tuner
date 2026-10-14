"""Post-success bookkeeping must not orphan the journal row (audit HIGH-2/3/7/13).

The wrapper writes a 'started' row *before* the delegate runs. When bookkeeping
fails after a successful compression, the old code dropped only the in-memory
pointer, leaving a row that can never be finalized -- so "no half-registered
pending operation is left behind" and "compute-then-commit" were both false.
These tests assert the *store* state, not just the pointer, and assert the host
still receives its compression even when error accounting itself is corrupt.
"""
from __future__ import annotations

from hermes_context_tuner import engine as engine_mod
from tests.test_engine_journal import FakeDelegate, make_engine

MSGS = [{"role": "user", "content": "hello world"}] * 5


def _boom(*args, **kwargs):
    raise RuntimeError("fingerprinting exploded")


def test_bookkeeping_failure_terminalizes_the_journal_row(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, FakeDelegate())
    engine._session_id = "s-old"
    monkeypatch.setattr(engine_mod, "_fingerprint_messages", _boom)

    result = engine.compress(MSGS, current_tokens=1_000)

    # the host still receives the compression it paid for
    assert len(result) == 1
    counts = engine._store.status_counts()
    # ...and no row is left claiming to still be in flight
    assert counts.get("started", 0) == 0, counts
    assert counts.get("interrupted", 0) == 1, counts
    assert engine._pending_operation is None


def test_bookkeeping_failure_with_corrupt_health_still_returns_result(tmp_path, monkeypatch):
    """Error accounting is wrapper-owned: if it raises, the compression survives."""
    engine = make_engine(tmp_path, FakeDelegate())
    engine._session_id = "s-old"
    engine._health = None  # malformed health must not defeat the boundary
    monkeypatch.setattr(engine_mod, "_fingerprint_messages", _boom)

    result = engine.compress(MSGS, current_tokens=1_000)

    assert len(result) == 1
    counts = engine._store.status_counts()
    assert counts.get("started", 0) == 0, counts
    assert counts.get("interrupted", 0) == 1, counts


def test_audit_failure_never_raises_on_corrupt_health(tmp_path):
    engine = make_engine(tmp_path, FakeDelegate())
    for corrupt in (None, {"audit_failures": "not-a-number"}, {"audit_failures": None}, 42):
        engine._health = corrupt
        engine._audit_failure()  # must not raise


def test_terminalize_is_a_noop_without_an_operation(tmp_path):
    engine = make_engine(tmp_path, FakeDelegate())
    engine._terminalize_operation(None, "nothing pending")  # must not raise
    assert engine._store.status_counts().get("started", 0) == 0
