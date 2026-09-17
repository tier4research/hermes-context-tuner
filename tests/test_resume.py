"""Resume packet tests (Task 12).

The session-continuity path: a restart/new session retrieves the intended
state without loading the full transcript. The packet is hard-capped and
carries only structured state.
"""
from __future__ import annotations

import json

from hermes_context_tuner.engine import RESUME_PACKET_CAP_BYTES

from tests.test_checkpoint_tools import make_engine  # noqa: F401  (re-export convenience)
from tests.test_checkpoint_utils import VALID_CHECKPOINT, make_engine as mk


def test_resume_packet_roundtrip_in_new_session(tmp_path):
    engine = mk(tmp_path)
    cid = engine.handle_tool_call("context_tuner_checkpoint", dict(VALID_CHECKPOINT))
    checkpoint_id = json.loads(cid)["checkpoint_id"]

    # "new session": a fresh engine instance over the same stores, asking for
    # the packet by project key (what the session-continuity skill does)
    fresh = mk(tmp_path)
    raw = fresh.handle_tool_call(
        "context_tuner_resume", {"project_key": VALID_CHECKPOINT["project_key"], "new_session_id": "brand-new-session"}
    )
    payload = json.loads(raw)
    assert payload["ok"] is True
    packet = payload["packet"]
    assert packet["checkpoint"]["checkpoint_id"] == checkpoint_id
    assert packet["checkpoint"]["goal"] == VALID_CHECKPOINT["goal"]
    assert packet["checkpoint"]["hard_constraints"] == VALID_CHECKPOINT["hard_constraints"]
    assert packet["checkpoint"]["next_actions"] == VALID_CHECKPOINT["next_actions"]
    assert packet["new_session_id"] == "brand-new-session"
    # packet carries the last compression pointer too
    assert "last_compression" not in packet or packet["last_compression"] is None or "id" in packet["last_compression"]


def _packet_bytes(packet):
    return len(json.dumps(packet, ensure_ascii=False, default=str).encode("utf-8"))


def test_resume_packet_is_bounded(tmp_path):
    engine = mk(tmp_path)
    big = dict(VALID_CHECKPOINT)
    big["completed"] = [f"completed item {i} with some detail text" for i in range(100)]
    engine.handle_tool_call("context_tuner_checkpoint", big)
    raw = engine.handle_tool_call("context_tuner_resume", {"project_key": big["project_key"]})
    packet = json.loads(raw)["packet"]
    # the packet itself must honour the hard cap, not the 2x envelope allowance
    assert _packet_bytes(packet) <= RESUME_PACKET_CAP_BYTES


def test_resume_packet_sheds_checkpoint_when_over_cap(tmp_path):
    engine = mk(tmp_path)
    # Largest legal checkpoint: under the 16 KB ledger cap but well over the
    # 8 KB packet cap.
    big = dict(VALID_CHECKPOINT)
    big["completed"] = ["c" * 900 for _ in range(8)]
    big["unresolved"] = ["u" * 900 for _ in range(8)]
    payload = json.loads(engine.handle_tool_call("context_tuner_checkpoint", big))
    assert payload["ok"] is True
    checkpoint_id = payload["checkpoint_id"]
    raw = engine.handle_tool_call(
        "context_tuner_resume", {"project_key": big["project_key"]}
    )
    packet = json.loads(raw)["packet"]
    assert _packet_bytes(packet) <= RESUME_PACKET_CAP_BYTES
    assert packet["truncated"] is True
    # identity survives even when the body is shed
    assert packet["checkpoint"]["checkpoint_id"] == checkpoint_id


def test_fit_packet_always_lands_under_cap(tmp_path):
    engine = mk(tmp_path)
    packet = {
        "project_key": "p",
        "conversation_id": None,
        "new_session_id": "s",
        "checkpoint": {
            "checkpoint_id": "abc",
            "created_at": 1.0,
            "goal": "g" * 1000,
            "current_stage": "c" * 1000,
            "status": "in_progress",
            "completed": ["x" * 1000] * 100,
            "artifacts": [{"path": "p" * 1000}] * 100,
        },
        "decisions": [{"content": "d" * 1000}] * 100,
        "tasks": {"t": {"status": "in_progress", "history_tail": ["n" * 300] * 5}},
    }
    fitted = engine._fit_packet(packet)
    assert _packet_bytes(fitted) <= RESUME_PACKET_CAP_BYTES
    assert fitted["truncated"] is True
    assert fitted["checkpoint"]["checkpoint_id"] == "abc"


def test_resume_rejects_oversized_project_key(tmp_path):
    engine = mk(tmp_path)
    raw = engine.handle_tool_call("context_tuner_resume", {"project_key": "k" * 5000})
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "too long" in payload["error"]


def test_resume_packet_never_contains_transcript_bodies(tmp_path):
    engine = mk(tmp_path)
    secret_body = "TRANSCRIPT-SENTENCE-THAT-MUST-NOT-LEAK-8842"
    # a compression happens with sensitive messages; the journal stores metadata
    engine._session_id = "s-old"
    engine.compress(
        [{"role": "user", "content": secret_body}], current_tokens=500
    )
    engine.on_session_start("s-new", boundary_reason="compression", old_session_id="s-old")
    engine.handle_tool_call("context_tuner_checkpoint", dict(VALID_CHECKPOINT))
    raw = engine.handle_tool_call(
        "context_tuner_resume", {"project_key": VALID_CHECKPOINT["project_key"]}
    )
    assert secret_body not in raw


def test_resume_by_conversation_filter(tmp_path):
    engine = mk(tmp_path)
    engine.handle_tool_call(
        "context_tuner_checkpoint",
        {**VALID_CHECKPOINT, "project_key": "p", "conversation_id": "conv-1", "goal": "goal one"},
    )
    engine.handle_tool_call(
        "context_tuner_checkpoint",
        {**VALID_CHECKPOINT, "project_key": "p", "conversation_id": "conv-2", "goal": "goal two"},
    )
    raw = engine.handle_tool_call(
        "context_tuner_resume", {"project_key": "p", "conversation_id": "conv-1"}
    )
    packet = json.loads(raw)["packet"]
    assert packet["checkpoint"]["goal"] == "goal one"
    raw_all = engine.handle_tool_call("context_tuner_resume", {"project_key": "p"})
    packet_all = json.loads(raw_all)["packet"]
    assert packet_all["checkpoint"]["goal"] == "goal two"


def test_resume_unknown_project_is_clean_empty(tmp_path):
    engine = mk(tmp_path)
    raw = engine.handle_tool_call("context_tuner_resume", {"project_key": "ghost"})
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["packet"]["checkpoint"] is None
    assert "decisions" not in payload["packet"] or payload["packet"]["decisions"] == []


def test_resume_requires_project_key(tmp_path):
    engine = mk(tmp_path)
    raw = engine.handle_tool_call("context_tuner_resume", {})
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "project_key" in payload["error"]
