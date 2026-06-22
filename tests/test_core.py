from hermes_context_tuner.budget import build_budget_plan
from hermes_context_tuner.recovery import RecoveryPointerStore


def test_budget_plan_classifies_head_tail_and_large_tool():
    messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "content": "x" * 9000},
        {"role": "user", "content": "recent"},
    ]
    plan = build_budget_plan(messages, context_budget=10000, tail_budget=20, protected_head=1, externalize_tool_threshold=1000)
    decisions = [d.decision for d in plan.decisions]
    assert decisions[0] == "keep_raw"
    assert decisions[2] == "externalize_hint"
    assert decisions[3] == "keep_raw"
    assert plan.total_tokens > 0


def test_recovery_store_records_event(tmp_path):
    store = RecoveryPointerStore(tmp_path / "ctx.sqlite")
    event_id = store.record_event(
        old_session_id="old",
        new_session_id="new",
        original_count=4,
        compressed_count=2,
        total_tokens=123,
        decisions=[{"index": 0, "role": "user", "rough_tokens": 10, "decision": "keep_raw", "reason": "head"}],
    )
    assert event_id == 1
    events = store.latest_events()
    assert events[0]["summary"] == "keep_raw=1"
    assert events[0]["old_session_id"] == "old"
