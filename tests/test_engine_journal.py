"""Engine boundary tests (Tasks 7/8): two-phase journal integration.

The wrapper must record the compression lifecycle without changing compressor
behavior. Uses a fake delegate with controllable success/failure and a
temporary SQLite store.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from hermes_context_tuner.budget import BudgetPolicy
from hermes_context_tuner.checkpoints import CheckpointLedger
from hermes_context_tuner.engine import (
    ContextTunerEngine,
    DelegateCompressionError,
    DelegateSignatureError,
    _invoke_compatible,
)
from hermes_context_tuner.recovery import RecoveryPointerStore


class FakeDelegate:
    """Shape-compatible stand-in for Hermes' ContextCompressor."""

    threshold_tokens = 50_000
    tail_token_budget = 10_000
    protect_first_n = 2

    def __init__(self, fail: bool = False, result: int = 1):
        self.fail = fail
        self.result_count = result
        self.compress_calls: list[dict[str, Any]] = []
        self.session_starts: list[dict[str, Any]] = []
        self.ended: list[tuple[str, list]] = []
        self.resets = 0

    def compress(self, messages, current_tokens=None, focus_topic=None, **kw):
        self.compress_calls.append(
            {"messages": messages, "current_tokens": current_tokens, "focus_topic": focus_topic, "kw": kw}
        )
        if self.fail:
            raise RuntimeError("delegate exploded")
        return [{"role": "user", "content": "compressed"}] * self.result_count

    def on_session_start(self, session_id, **kwargs):
        self.session_starts.append({"session_id": session_id, **kwargs})

    def on_session_end(self, session_id, messages):
        self.ended.append((session_id, messages))

    def on_session_reset(self):
        self.resets += 1

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


def test_compress_starts_operation_and_delegates_once(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    msgs = [{"role": "user", "content": "hello world"}] * 5
    result = engine.compress(msgs, current_tokens=1_000)
    assert len(delegate.compress_calls) == 1
    assert len(result) == 1
    # exactly one started operation, nothing finalized yet
    counts = engine._store.status_counts()
    assert counts.get("started") == 1
    assert counts.get("finalized", 0) == 0
    assert engine._pending_operation is not None
    assert engine._pending_count == 1


def test_success_not_finalized_until_host_callback(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}] * 3, current_tokens=100)
    assert engine._store.status_counts().get("finalized", 0) == 0
    # host reports the committed compression boundary
    engine.on_session_start(
        "s-new",
        boundary_reason="compression",
        old_session_id="s-old",
        conversation_id="conv-1",
    )
    counts = engine._store.status_counts()
    assert counts.get("finalized") == 1
    assert counts.get("started", 0) == 0
    ev = engine._store.latest_events(1)[0]
    assert ev["old_session_id"] == "s-old"
    assert ev["new_session_id"] == "s-new"
    assert ev["conversation_id"] == "conv-1"
    assert ev["finalized_at"] is not None


def test_on_session_start_finalizes_exactly_once(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    engine.on_session_start("s-new", boundary_reason="compression", old_session_id="s-old")
    # duplicate callback: no second finalization, no error
    engine.on_session_start("s-new-2", boundary_reason="compression", old_session_id="s-old")
    assert engine._store.status_counts().get("finalized") == 1


def test_delegate_exception_records_failure_and_propagates(tmp_path):
    delegate = FakeDelegate(fail=True)
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    with pytest.raises(RuntimeError, match="delegate exploded"):
        engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    counts = engine._store.status_counts()
    assert counts.get("failed") == 1
    assert counts.get("finalized", 0) == 0
    ev = engine._store.latest_events(1)[0]
    assert ev["status"] == "failed"
    # content-free: only the exception class is persisted, never its message
    assert "RuntimeError" in (ev["checkpoint_reason"] or "")
    assert "exploded" not in (ev["checkpoint_reason"] or "")


def test_missing_callback_leaves_pending_marked_interrupted_at_teardown(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    op = engine._pending_operation
    # no boundary callback ever arrives; teardown sweep runs
    engine._close_pending_on_teardown(reason="engine torn down without boundary callback")
    assert engine._store.status_counts().get("interrupted") == 1
    assert engine._store.latest_pending() is None
    assert engine._pending_operation is None
    _ = op


def test_incompatible_session_start_interrupts_stale_pending(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    # a NEW normal session start (no compression boundary) arrives while an
    # operation is still pending: the old operation can never finalize.
    engine.on_session_start("brand-new-session")
    assert engine._store.status_counts().get("interrupted") == 1
    assert engine._store.status_counts().get("finalized", 0) == 0


def test_duplicate_callbacks_do_not_duplicate_finalization(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    for _ in range(3):
        engine.on_session_start("s-new", boundary_reason="compression", old_session_id="s-old")
    counts = engine._store.status_counts()
    assert counts.get("finalized") == 1
    # delegate still notified once per callback (host contract), engine stable
    assert len(delegate.session_starts) == 3


def test_second_compression_without_boundary_interrupts_first(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    first_op = engine._pending_operation
    # a second compression starts with no boundary callback in between: the
    # first operation can never be finalized and must not be orphaned silently
    engine.compress([{"role": "user", "content": "y"}], current_tokens=10)
    counts = engine._store.status_counts()
    assert counts.get("interrupted") == 1
    assert counts.get("started") == 1
    assert engine._pending_operation != first_op
    # teardown closes the remaining one
    engine._close_pending_on_teardown("end")
    assert engine._store.status_counts().get("interrupted") == 2


def test_store_failure_does_not_break_compression(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)

    class BadStore:
        def begin_compression(self, **kwargs):
            raise sqlite3.Error("disk on fire")

    engine._store = BadStore()
    result = engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    assert len(result) == 1
    assert engine.get_status()["health"]["audit_failures"] == 1


def test_conversation_id_carried_separately_from_session_ids(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    engine.on_session_start(
        "s-new",
        boundary_reason="compression",
        old_session_id="s-old",
        conversation_id="gw-key-42",
        platform="telegram",
    )
    ev = engine._store.latest_events(1)[0]
    assert ev["old_session_id"] == "s-old"
    assert ev["new_session_id"] == "s-new"
    assert ev["conversation_id"] == "gw-key-42"


def test_delegate_body_typeerror_is_wrapped_not_masked(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)

    def bad_compress(messages, current_tokens=None):
        raise TypeError("internal delegate bug")

    engine._delegate.compress = bad_compress
    with pytest.raises(DelegateCompressionError):
        engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    # the failed attempt is recorded as failed, not finalized
    assert engine._store.status_counts().get("failed") == 1


def test_engine_passthrough_of_delegate_context_fields():
    engine = ContextTunerEngine.__new__(ContextTunerEngine)
    d = FakeDelegate()
    d.context_length = 131_072
    engine._delegate = d
    assert engine.context_length == 131_072
    with pytest.raises(AttributeError):
        engine.definitely_not_a_real_attribute


def test_session_end_and_reset_close_pending(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    engine.on_session_end("s-old", [])
    assert engine._store.status_counts().get("interrupted") == 1
    engine.compress([{"role": "user", "content": "y"}], current_tokens=10)
    engine.on_session_reset()
    assert engine._store.status_counts().get("interrupted") == 2


def test_finalize_failure_leaves_no_non_terminal_operation(tmp_path):
    """A failed finalize must close the op (interrupted), never leave it started."""
    engine = make_engine(tmp_path, FakeDelegate())
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    real_store = engine._store

    class FlakyFinalize:
        def __getattr__(self, name):
            return getattr(real_store, name)

        def finalize_compression(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    engine._store = FlakyFinalize()
    engine.on_session_start(
        "s-new", boundary_reason="compression", old_session_id="s-old"
    )
    counts = real_store.status_counts()
    assert counts.get("started", 0) == 0, "operation must not be orphaned in 'started'"
    assert counts.get("interrupted") == 1
    assert counts.get("finalized", 0) == 0


def test_boundary_parent_mismatch_does_not_finalize(tmp_path):
    """A boundary for a different parent session must not be attributed here."""
    engine = make_engine(tmp_path, FakeDelegate())
    engine._session_id = "s-old"
    engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    engine.on_session_start(
        "s-new",
        boundary_reason="compression",
        old_session_id="some-other-session",
    )
    counts = engine._store.status_counts()
    assert counts.get("finalized", 0) == 0
    assert counts.get("interrupted") == 1


def test_stale_started_reconciled_but_live_untouched(tmp_path):
    store = RecoveryPointerStore(tmp_path / "stale.sqlite")
    fresh_op = store.begin_compression(
        old_session_id="live", original_count=1, total_tokens=1
    )
    old_op = store.begin_compression(
        old_session_id="dead", original_count=1, total_tokens=1
    )
    con = store._connect()
    try:
        con.execute(
            "UPDATE compression_events SET started_at = started_at - 100000"
            " WHERE operation_id = ?",
            (old_op,),
        )
        con.commit()
    finally:
        con.close()

    changed = store.interrupt_stale_started(max_age_seconds=3600)
    assert changed == 1
    counts = store.status_counts()
    assert counts.get("interrupted") == 1
    assert counts.get("started") == 1
    assert store.latest_pending()["operation_id"] == fresh_op


def test_mark_interrupted_if_started_is_state_guarded(tmp_path):
    store = RecoveryPointerStore(tmp_path / "guard.sqlite")
    op = store.begin_compression(old_session_id="s", original_count=1, total_tokens=1)
    store.finalize_compression(op, new_session_id="n", compressed_count=1)
    # already terminal: must not raise and must not change the row
    assert store.mark_interrupted_if_started(op, reason="late") is False
    assert store.status_counts().get("finalized") == 1


def test_delegate_exception_message_never_persisted(tmp_path):
    secret = "TRANSCRIPT-SECRET-XX-7788"

    class Boom(RuntimeError):
        pass

    engine = make_engine(tmp_path, FakeDelegate())

    def bad(messages, current_tokens=None):
        raise Boom(f"failed while summarizing: {secret}")

    engine._delegate.compress = bad
    engine._session_id = "s"
    with pytest.raises(Exception):
        engine.compress([{"role": "user", "content": secret}], current_tokens=10)
    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"engine.sqlite{suffix}"
        if path.exists():
            assert secret.encode() not in path.read_bytes()


def test_focus_topic_text_never_persisted(tmp_path):
    secret_topic = "SECRET-FOCUS-TOPIC-9911"
    engine = make_engine(tmp_path, FakeDelegate())
    engine._session_id = "s"
    engine.compress(
        [{"role": "user", "content": "x" * 4000}] * 3,
        current_tokens=10,
        focus_topic=secret_topic,
    )
    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"engine.sqlite{suffix}"
        if path.exists():
            assert secret_topic.encode() not in path.read_bytes()
    con = engine._store._connect()
    try:
        row = con.execute(
            "SELECT trigger_json FROM compression_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    assert secret_topic not in (row[0] or "")
    assert json.loads(row[0])["focus_topic_present"] is True


def test_checkpoint_required_only_after_repeated_lossy_compressions(tmp_path):
    engine = make_engine(tmp_path, FakeDelegate())
    engine._ledger = CheckpointLedger(tmp_path / "cp.sqlite")
    engine._session_id = "s-0"
    # long messages so middle context is genuinely summarized (lossy)
    msgs = [{"role": "user", "content": "x" * 1000}] * 60
    required = []
    for i in range(4):
        old = engine._session_id
        engine.compress(msgs, current_tokens=1_000)
        engine.on_session_start(
            f"s-{i + 1}", boundary_reason="compression", old_session_id=old
        )
        required.append(engine._store.latest_events(1)[0]["checkpoint_required"])
    assert required[:2] == [0, 0], "a single lossy compression is not a refresh signal"
    assert required[2] == 1, "repeated lossy compression without a checkpoint must signal"


def test_explicit_checkpoint_resets_the_refresh_signal(tmp_path):
    engine = make_engine(tmp_path, FakeDelegate())
    engine._ledger = CheckpointLedger(tmp_path / "cp.sqlite")
    engine._session_id = "s-0"
    msgs = [{"role": "user", "content": "x" * 1000}] * 60
    for i in range(2):
        old = engine._session_id
        engine.compress(msgs, current_tokens=1_000)
        engine.on_session_start(
            f"s-{i + 1}", boundary_reason="compression", old_session_id=old
        )
    assert engine._compressions_since_checkpoint == 2
    engine.handle_tool_call(
        "context_tuner_checkpoint",
        {
            "project_key": "p",
            "goal": "g",
            "current_stage": "c",
            "status": "in_progress",
        },
    )
    assert engine._compressions_since_checkpoint == 0


def test_budget_policy_is_used_for_the_checkpoint_signal(tmp_path):
    policy = BudgetPolicy(
        context_budget=100_000,
        recent_conversation=10_000,
        source_material=0,
        relevant_retrieval=0,
        project_state=0,
        current_work=0,
        output_reserve=0,
    )
    engine = make_engine(tmp_path, FakeDelegate())
    engine._budget_policy = policy
    engine._session_id = "s-old"
    msgs = [{"role": "user", "content": "x" * 1000}] * 60
    engine.compress(msgs, current_tokens=9_900)
    engine.on_session_start("s-1", boundary_reason="compression", old_session_id="s-old")
    assert engine._store.latest_events(1)[0]["checkpoint_required"] == 1


def test_signature_mismatch_is_distinct_from_body_typeerror(tmp_path):
    class Strict:
        def compress(self, messages, *, required_kw):
            return []

    engine = make_engine(tmp_path, Strict())
    engine._session_id = "s"
    with pytest.raises(DelegateSignatureError):
        engine.compress([{"role": "user", "content": "x"}], current_tokens=10)
    ev = engine._store.latest_events(1)[0]
    assert ev["status"] == "failed"
    assert ev["checkpoint_reason"] == "unsupported delegate signature"


def test_uninspectable_delegate_is_still_called(tmp_path):
    class Uninspectable:
        def __init__(self):
            self.calls = []

        def __call__(self, messages, current_tokens=None, focus_topic=None, **kw):
            self.calls.append(current_tokens)
            return [{"role": "user", "content": "ok"}]

        @property
        def __signature__(self):
            raise ValueError("opaque callable")

    delegate = FakeDelegate()
    opaque = Uninspectable()
    delegate.compress = opaque
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s"
    result = engine.compress([{"role": "user", "content": "x"}], current_tokens=42)
    assert len(result) == 1
    assert opaque.calls == [42]


def test_invoke_compatible_falls_back_when_signature_is_opaque():
    calls = []

    class Uninspectable:
        def __call__(self, messages, current_tokens=None, focus_topic=None, **kw):
            calls.append(current_tokens)
            return ["ok"]

        @property
        def __signature__(self):
            raise ValueError("opaque callable")

    result = _invoke_compatible(
        Uninspectable(), [(([],), {"current_tokens": 5}), (([],), {})]
    )
    assert result == ["ok"]
    assert calls == [5]


def test_getattr_does_not_recurse_without_delegate():
    engine = ContextTunerEngine.__new__(ContextTunerEngine)
    with pytest.raises(AttributeError):
        engine._delegate


def test_status_tool_tolerates_bad_limit(tmp_path):
    engine = make_engine(tmp_path, FakeDelegate())
    for bad in ({"limit": None}, {"limit": "five"}, {"limit": [1]}):
        payload = json.loads(engine.handle_tool_call("context_tuner_status", bad))
        assert payload["status"]["engine"] == "context-tuner"
