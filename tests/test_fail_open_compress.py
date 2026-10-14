"""Regression: wrapper-side failures must never deny or discard a compression.

Sol's tuner audit (H-05) found the wrapper was not truly fail-open: budget
planning sat *before* any try/except, and post-delegate bookkeeping ran *after*
the delegate had already succeeded. A wrapper bug in either place could raise —
and in the second case it would throw away a compression that actually worked.

Contract under test: the wrapper may lose its audit trail, never the host's
compression. A wrapper-side failure degrades to passthrough.
"""
from __future__ import annotations

from typing import Any

import pytest

import hermes_context_tuner.engine as engine_mod
from hermes_context_tuner.engine import ContextTunerEngine
from hermes_context_tuner.recovery import RecoveryPointerStore


class FakeDelegate:
    """Shape-compatible stand-in for Hermes' ContextCompressor."""

    threshold_tokens = 50_000
    tail_token_budget = 10_000
    protect_first_n = 2

    def __init__(self, result: int = 2):
        self.result_count = result
        self.compress_calls = 0

    def compress(self, messages, current_tokens=None, focus_topic=None, **kw):
        self.compress_calls += 1
        return [{"role": "user", "content": "compressed"}] * self.result_count

    def on_session_start(self, session_id, **kwargs):
        pass

    def on_session_end(self, session_id, messages):
        pass

    def on_session_reset(self):
        pass

    def get_status(self):
        return {"delegate": True}


def make_engine(tmp_path, delegate: FakeDelegate | None = None) -> ContextTunerEngine:
    engine = ContextTunerEngine.__new__(ContextTunerEngine)
    engine._delegate = delegate or FakeDelegate()
    engine._store = RecoveryPointerStore(tmp_path / "engine.sqlite")
    engine._ledger = None
    engine._last_plan = None
    engine._session_id = ""
    engine._pending_operation = None
    engine._pending_count = 0
    engine._pending_result_fingerprint = None
    engine._pending_checkpoint_hint = False
    engine._pending_old_session_id = None
    engine._budget_policy = None
    engine._compressions_since_checkpoint = 0
    engine._health = {"audit_failures": 0, "recovery_failures": 0}
    return engine


MSGS = [{"role": "user", "content": "hello world"}] * 5


def test_planning_failure_still_compresses(tmp_path, monkeypatch):
    """A raise inside build_budget_plan must not deny the compression."""
    def boom(*a, **kw):
        raise RuntimeError("planning exploded")

    monkeypatch.setattr(engine_mod, "build_budget_plan", boom)
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)

    result = engine.compress(MSGS, current_tokens=1_000)

    assert delegate.compress_calls == 1, "delegate must still be invoked once"
    assert len(result) == 2
    assert engine._health["audit_failures"] == 1
    assert engine._last_plan is None


def test_planning_failure_never_writes_a_zeroed_journal_row(tmp_path, monkeypatch):
    """No plan means nothing truthful to journal: skip the write, do not zero it."""
    def boom(*a, **kw):
        raise RuntimeError("planning exploded")

    monkeypatch.setattr(engine_mod, "build_budget_plan", boom)
    engine = make_engine(tmp_path)

    engine.compress(MSGS, current_tokens=1_000)

    counts = engine._store.status_counts()
    assert counts.get("started", 0) == 0
    assert counts.get("finalized", 0) == 0
    assert engine._pending_operation is None


def test_bookkeeping_failure_returns_the_delegate_result(tmp_path, monkeypatch):
    """A raise after a successful delegate call must not discard that success."""
    def boom(*a, **kw):
        raise RuntimeError("fingerprint exploded")

    monkeypatch.setattr(engine_mod, "_fingerprint_messages", boom)
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)

    result = engine.compress(MSGS, current_tokens=1_000)

    assert delegate.compress_calls == 1
    assert len(result) == 2, "the compression that worked must still be returned"
    assert engine._health["audit_failures"] == 1
    # No half-registered pending operation left behind.
    assert engine._pending_operation is None
    assert engine._pending_result_fingerprint is None
    assert engine._pending_checkpoint_hint is False


def test_checkpoint_hint_failure_does_not_discard_result(tmp_path, monkeypatch):
    """A raise in _checkpoint_refresh_needed is wrapper-side, not fatal."""

    class _Decision:
        def __init__(self, decision):
            self.decision = decision

    class _Plan:
        total_tokens = 1_000
        decisions = [_Decision("summarize")]

    def fake_plan(*a, **kw):
        return _Plan()

    def boom(self, current_tokens):
        raise RuntimeError("budget policy exploded")

    monkeypatch.setattr(engine_mod, "build_budget_plan", fake_plan)
    monkeypatch.setattr(ContextTunerEngine, "_checkpoint_refresh_needed", boom)
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._budget_policy = object()

    result = engine.compress(MSGS, current_tokens=1_000)

    assert len(result) == 2
    assert engine._health["audit_failures"] == 1
    assert engine._pending_operation is None


def test_teardown_failure_does_not_block_the_next_compression(tmp_path, monkeypatch):
    """An orphaned pending op whose close fails must not wedge compress()."""
    def boom(self, reason):
        raise RuntimeError("teardown exploded")

    monkeypatch.setattr(ContextTunerEngine, "_close_pending_on_teardown", boom)
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._pending_operation = "orphan-op-id"

    result = engine.compress(MSGS, current_tokens=1_000)

    assert len(result) == 2
    assert engine._health["audit_failures"] == 1
    # The unwedgeable orphan is dropped rather than carried forever.
    assert engine._pending_operation != "orphan-op-id"


def test_happy_path_still_journals_and_sets_pending(tmp_path):
    """The fail-open changes must not weaken the normal recording path."""
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"

    result = engine.compress(MSGS, current_tokens=1_000)

    assert len(result) == 2
    assert engine._health["audit_failures"] == 0
    assert engine._last_plan is not None
    assert engine._pending_operation is not None
    assert engine._pending_count == 2
    assert engine._store.status_counts().get("started", 0) == 1
