"""Pointer/fingerprint accounting tests (Task 9).

The journal must tell the truth about what was kept, summarized, or
externalized — via exact indexes, ranges, roles, token metadata, and content
fingerprints — while never persisting message bodies, secrets, or prose.
"""
from __future__ import annotations

import json

from hermes_context_tuner.budget import build_budget_plan
from hermes_context_tuner.engine import ContextTunerEngine
from hermes_context_tuner.recovery import RecoveryPointerStore, message_fingerprint

from tests.test_engine_journal import FakeDelegate, make_engine


def test_exact_index_range_role_token_accounting(tmp_path):
    store = RecoveryPointerStore(tmp_path / "p.sqlite")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "follow-up"},
    ]
    decisions = [
        {"index": 0, "role": "system", "rough_tokens": 8, "decision": "keep_raw", "reason": "head"},
        {"index": 1, "role": "user", "rough_tokens": 12, "decision": "summarize", "reason": "middle"},
        {"index": 2, "role": "assistant", "rough_tokens": 10, "decision": "summarize", "reason": "middle"},
        {"index": 3, "role": "user", "rough_tokens": 9, "decision": "keep_raw", "reason": "tail"},
    ]
    op = store.begin_compression(
        old_session_id="s-old",
        original_count=4,
        total_tokens=39,
        messages=messages,
        decisions=decisions,
    )
    store.finalize_compression(op, new_session_id="s-new", compressed_count=2)
    event = store.latest_events(1)[0]
    pointers = store.event_pointers(event["id"])
    assert len(pointers) == 4
    by_index = {p["message_index"]: p for p in pointers}
    assert by_index[0]["role"] == "system"
    assert by_index[0]["decision"] == "keep_raw"
    assert by_index[0]["range_start"] == 0 and by_index[0]["range_end"] == 1
    assert by_index[1]["rough_tokens"] == 12
    assert by_index[1]["decision"] == "summarize"
    assert by_index[3]["range_start"] == 3 and by_index[3]["range_end"] == 4
    # every pointer carries a fingerprint matching its exact source message
    for p in pointers:
        expected = message_fingerprint(messages[p["message_index"]])
        assert p["fingerprint"] == expected
        assert p["fingerprint"] != ""


def test_verify_pointers_available_iff_source_matches(tmp_path):
    store = RecoveryPointerStore(tmp_path / "v.sqlite")
    messages = [
        {"role": "user", "content": "original message text"},
        {"role": "assistant", "content": "reply"},
    ]
    op = store.begin_compression(
        old_session_id="s",
        original_count=2,
        total_tokens=10,
        messages=messages,
        decisions=[
            {"index": 0, "role": "user", "rough_tokens": 6, "decision": "summarize", "reason": "r"},
            {"index": 1, "role": "assistant", "rough_tokens": 4, "decision": "summarize", "reason": "r"},
        ],
    )
    rows = store.verify_pointers(op, messages)
    assert all(r["available"] for r in rows)
    # source mutated: fingerprint no longer matches
    mutated = [{"role": "user", "content": "CHANGED"}, {"role": "assistant", "content": "reply"}]
    rows = store.verify_pointers(op, mutated)
    assert rows[0]["available"] is False
    assert rows[1]["available"] is True
    # source gone entirely
    rows = store.verify_pointers(op, [])
    assert all(r["available"] is False for r in rows)


def test_summary_marker_pointer_recorded_on_finalize(tmp_path):
    store = RecoveryPointerStore(tmp_path / "m.sqlite")
    op = store.begin_compression(old_session_id="s", original_count=1, total_tokens=5)
    store.finalize_compression(
        op,
        new_session_id="n",
        compressed_count=1,
        summary_marker="summary://session-n/idx-0",
        checkpoint_required=True,
        checkpoint_reason="decision content externalized",
    )
    ev = store.latest_events(1)[0]
    assert ev["summary_marker"] == "summary://session-n/idx-0"
    assert ev["checkpoint_required"] == 1
    assert "externalized" in ev["checkpoint_reason"]


def test_empty_and_noop_compression_recorded_honestly(tmp_path):
    store = RecoveryPointerStore(tmp_path / "e.sqlite")
    # zero decisions
    op = store.begin_compression(old_session_id="s", original_count=0, total_tokens=0)
    store.finalize_compression(op, new_session_id="n", compressed_count=0)
    ev = store.latest_events(1)[0]
    assert ev["summary"] == "no decisions"
    assert ev["pre_count"] == 0 and ev["post_count"] == 0
    assert store.event_pointers(ev["id"]) == []


def test_bounded_pointer_storage(tmp_path):
    store = RecoveryPointerStore(tmp_path / "b.sqlite")
    n = 12_000  # exceeds MAX_POINTER_ROWS_PER_EVENT
    messages = [{"role": "user", "content": f"m{i}"} for i in range(n)]
    decisions = [
        {"index": i, "role": "user", "rough_tokens": 1, "decision": "keep_raw", "reason": "r"}
        for i in range(n)
    ]
    op = store.begin_compression(
        old_session_id="s", original_count=n, total_tokens=n, messages=messages, decisions=decisions
    )
    store.finalize_compression(op, new_session_id="n", compressed_count=n)
    ev = store.latest_events(1)[0]
    assert len(store.event_pointers(ev["id"], limit=1_000_000)) <= 10_000


def test_sensitive_bodies_never_enter_sidecar(tmp_path):
    store = RecoveryPointerStore(tmp_path / "s.sqlite")
    secrets = [
        "credential-super-secret-123",
        "api_key = sk-REALLY-NOT-A-REAL-KEY",
        "password hunter2 topsecret",
    ]
    messages = [{"role": "user", "content": s} for s in secrets]
    decisions = [
        {"index": i, "role": "user", "rough_tokens": 10, "decision": "summarize", "reason": "budget"}
        for i in range(len(secrets))
    ]
    op = store.begin_compression(
        old_session_id="s", original_count=3, total_tokens=30, messages=messages, decisions=decisions
    )
    store.finalize_compression(op, new_session_id="n", compressed_count=1)
    raw = (tmp_path / "s.sqlite").read_bytes()
    for s in secrets:
        assert s.encode("utf-8") not in raw, f"body leaked into sidecar: {s}"
    # WAL sidecars too
    for suffix in ("-wal", "-shm"):
        p = tmp_path / f"s.sqlite{suffix}"
        if p.exists():
            for s in secrets:
                assert s.encode("utf-8") not in p.read_bytes()


def test_engine_journal_records_plan_accounting_end_to_end(tmp_path):
    messages = [
        {"role": "system", "content": "You are Hermes."},
        {"role": "tool", "content": "x" * 9000},
        {"role": "assistant", "content": "y" * 50},
        {"role": "user", "content": "recent question"},
    ]
    plan = build_budget_plan(messages, context_budget=1000, tail_budget=30, protected_head=1)
    assert any(d.decision == "externalize_hint" for d in plan.decisions)
    delegate = FakeDelegate(result=2)
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress(messages, current_tokens=5000)
    engine.on_session_start("s-new", boundary_reason="compression", old_session_id="s-old")
    ev = engine._store.latest_events(1)[0]
    assert ev["pre_count"] == 4
    assert ev["post_count"] == 2
    assert ev["status"] == "finalized"
    # trigger metadata present, no message bodies
    con = engine._store._connect()
    try:
        trigger = con.execute(
            "SELECT trigger_json FROM compression_events WHERE id = ?", (ev["id"],)
        ).fetchone()[0]
    finally:
        con.close()
    t = json.loads(trigger)
    assert t["current_tokens"] == 5000
    assert "x" * 100 not in trigger


def test_fingerprint_is_stable_and_covers_content():
    m1 = {"role": "user", "content": "same"}
    m2 = {"role": "user", "content": "same"}
    m3 = {"role": "user", "content": "different"}
    assert message_fingerprint(m1) == message_fingerprint(m2)
    assert message_fingerprint(m1) != message_fingerprint(m3)
    # key order does not matter
    assert message_fingerprint({"role": "user", "content": "z"}) == message_fingerprint(
        {"content": "z", "role": "user"}
    )


def test_verify_pointers_survive_reopen(tmp_path):
    store = RecoveryPointerStore(tmp_path / "r.sqlite")
    messages = [{"role": "user", "content": "persist me"}]
    op = store.begin_compression(
        old_session_id="s",
        original_count=1,
        total_tokens=5,
        messages=messages,
        decisions=[{"index": 0, "role": "user", "rough_tokens": 5, "decision": "keep_raw", "reason": "r"}],
    )
    store2 = RecoveryPointerStore(tmp_path / "r.sqlite")
    rows = store2.verify_pointers(op, messages)
    assert len(rows) == 1 and rows[0]["available"] is True


def test_source_fingerprint_covers_content_not_just_counts(tmp_path):
    store = RecoveryPointerStore(tmp_path / "fp.sqlite")
    decisions = [
        {"index": i, "role": "user", "rough_tokens": 1, "decision": "keep_raw", "reason": "r"}
        for i in range(2)
    ]
    msgs_a = [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}]
    msgs_b = [{"role": "user", "content": "one"}, {"role": "user", "content": "TWO"}]
    op_a = store.begin_compression(
        old_session_id="s", original_count=2, total_tokens=2, messages=msgs_a, decisions=decisions
    )
    op_b = store.begin_compression(
        old_session_id="s", original_count=2, total_tokens=2, messages=msgs_b, decisions=decisions
    )
    con = store._connect()
    try:
        rows = {
            r["operation_id"]: r["source_fingerprint"]
            for r in con.execute(
                "SELECT operation_id, source_fingerprint FROM compression_events"
            )
        }
    finally:
        con.close()
    # identical count and token total, different content -> different fingerprint
    assert rows[op_a]
    assert rows[op_a] != rows[op_b]
