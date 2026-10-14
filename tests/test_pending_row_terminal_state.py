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


def test_successful_compression_is_never_recorded_as_failed(tmp_path, monkeypatch):
    """Recovery must not lie in the other direction either.

    Terminalizing with ``fail_compression()`` would record a compression the
    delegate delivered as 'failed'. 'Interrupted' is the only honest state for
    "the work happened, its completion record did not" (audit HIGH-14).
    """
    engine = make_engine(tmp_path, FakeDelegate())
    engine._session_id = "s-old"
    calls = []
    monkeypatch.setattr(
        engine._store, "fail_compression", lambda *a, **k: calls.append(a)
    )
    monkeypatch.setattr(engine_mod, "_fingerprint_messages", _boom)

    engine.compress(MSGS, current_tokens=1_000)

    assert calls == []
    assert engine._store.status_counts().get("interrupted", 0) == 1


def test_pending_state_is_cleared_even_when_terminalization_raises(tmp_path, monkeypatch):
    """Cleanup runs in a finally: a raising terminalize must not leave a live
    pointer at a row that is now terminal (audit HIGH-16)."""
    engine = make_engine(tmp_path, FakeDelegate())
    engine._session_id = "s-old"
    engine._pending_operation = "stale-op"
    engine._pending_result_fingerprint = "deadbeef"

    def _store_gone(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(engine._store, "mark_interrupted_if_started", _store_gone)
    monkeypatch.setattr(engine_mod, "_fingerprint_messages", _boom)

    result = engine.compress(MSGS, current_tokens=1_000)

    assert len(result) == 1
    assert engine._pending_operation is None
    assert engine._pending_result_fingerprint is None


def test_audit_failure_does_not_mutate_delegate_state(tmp_path):
    """__getattr__ forwards unknown reads to the delegate, so an unbound
    ``_health`` would otherwise hand error accounting the delegate's own
    dictionary to increment (audit MEDIUM: delegate-owned state mutated)."""
    engine = make_engine(tmp_path, FakeDelegate())
    engine._delegate._health = {"audit_failures": 0}
    object.__delattr__(engine, "_health")  # unknown read now delegates

    engine._audit_failure()

    assert engine._delegate._health == {"audit_failures": 0}


def test_plan_is_kept_when_trigger_metadata_cannot_be_built(tmp_path, monkeypatch):
    """A hostile focus_topic must not cost the compression its journal row: the
    row is provenance for work that still happens (audit HIGH-15)."""
    engine = make_engine(tmp_path, FakeDelegate())
    engine._session_id = "s-old"
    seen = []
    real = engine._store.begin_compression

    def _spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(engine._store, "begin_compression", _spy)

    class _HostileLen:
        def __len__(self):
            raise RuntimeError("hostile focus_topic")

    engine.compress(MSGS, current_tokens=1_000, focus_topic=_HostileLen())

    assert seen, "the journal row must still be opened"
    assert seen[0]["trigger"].get("metadata_degraded") is True
