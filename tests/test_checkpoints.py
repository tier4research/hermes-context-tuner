"""Explicit checkpoint ledger tests (Task 11).

Semantic state enters the sidecar ONLY through this explicit, bounded,
append-only API (plan rule 6). Corrections supersede; they never mutate.
Project/conversation scoping prevents cross-project resume leakage.
"""
from __future__ import annotations

import copy
import sqlite3

import pytest

from hermes_context_tuner.checkpoints import CheckpointLedger

VALID_CHECKPOINT = {
    "project_key": "proj-continuity",
    "task_id": "task-42",
    "goal": "finish the journal migration",
    "current_stage": "review",
    "status": "in_progress",
    "completed": ["schema v3 migration implemented"],
    "locked_decisions": ["user_version is the schema carrier"],
    "hard_constraints": ["never store transcript bodies"],
    "unresolved": ["independent review pending"],
    "next_actions": ["run full suite from gateway interpreter"],
    "artifacts": [{"path": "src/hermes_context_tuner/recovery.py", "sha256": "a" * 64}],
    "provenance": "written by Andre during journal implementation",
}


def _ledger(tmp_path) -> CheckpointLedger:
    return CheckpointLedger(tmp_path / "cp.sqlite")


def test_checkpoint_write_read_roundtrip(tmp_path):
    led = _ledger(tmp_path)
    cid = led.write_checkpoint(VALID_CHECKPOINT)
    assert isinstance(cid, str) and cid
    state = led.latest_state("proj-continuity")
    assert state["checkpoint"]["checkpoint_id"] == cid
    for key in ("goal", "current_stage", "status", "provenance"):
        assert state["checkpoint"][key] == VALID_CHECKPOINT[key]
    assert state["checkpoint"]["completed"] == VALID_CHECKPOINT["completed"]
    assert state["checkpoint"]["artifacts"] == VALID_CHECKPOINT["artifacts"]


def test_required_fields_and_status_validation(tmp_path):
    led = _ledger(tmp_path)
    bad = copy.deepcopy(VALID_CHECKPOINT)
    del bad["project_key"]
    with pytest.raises(ValueError, match="project_key"):
        led.write_checkpoint(bad)
    bad2 = copy.deepcopy(VALID_CHECKPOINT)
    bad2["status"] = "vibes"
    with pytest.raises(ValueError, match="status"):
        led.write_checkpoint(bad2)
    bad3 = copy.deepcopy(VALID_CHECKPOINT)
    bad3["goal"] = ""
    with pytest.raises(ValueError, match="goal"):
        led.write_checkpoint(bad3)
    # nothing written by any rejection
    assert led.latest_state("proj-continuity") is None


def test_payload_caps_enforced_atomically(tmp_path):
    led = _ledger(tmp_path)
    # oversized list
    big = copy.deepcopy(VALID_CHECKPOINT)
    big["completed"] = [f"item {i}" for i in range(500)]
    with pytest.raises(ValueError, match="too many"):
        led.write_checkpoint(big)
    # oversized string
    big2 = copy.deepcopy(VALID_CHECKPOINT)
    big2["goal"] = "x" * 10_000
    with pytest.raises(ValueError, match="too long"):
        led.write_checkpoint(big2)
    # oversized total payload
    big3 = copy.deepcopy(VALID_CHECKPOINT)
    big3["unresolved"] = ["y" * 400] * 50
    with pytest.raises(ValueError):
        led.write_checkpoint(big3)
    # atomic: nothing persisted
    assert led.latest_state("proj-continuity") is None
    assert led.status_counts() == {}


def test_malformed_artifact_reference_rejected(tmp_path):
    led = _ledger(tmp_path)
    bad = copy.deepcopy(VALID_CHECKPOINT)
    bad["artifacts"] = [{"path": "x.py", "sha256": "not-a-hash"}]
    with pytest.raises(ValueError, match="sha256"):
        led.write_checkpoint(bad)
    bad2 = copy.deepcopy(VALID_CHECKPOINT)
    bad2["artifacts"] = [{"sha256": "a" * 64}]
    with pytest.raises(ValueError, match="path"):
        led.write_checkpoint(bad2)


def test_decision_append_and_supersession_never_mutates(tmp_path):
    led = _ledger(tmp_path)
    d1 = led.append_decision(
        "proj-continuity", "use user_version for schema versioning", source="session A"
    )
    d2 = led.append_decision(
        "proj-continuity",
        "use schema_info table for schema versioning",
        supersedes=d1,
        source="correction",
    )
    assert d1 != d2
    state = led.latest_state("proj-continuity")
    active_ids = [d["decision_id"] for d in state["decisions"]]
    assert d2 in active_ids and d1 not in active_ids
    # append-only: old row still present, unmutated
    con = sqlite3.connect(str(led.path))
    rows = con.execute(
        "SELECT decision_id, content, supersedes FROM decision_events ORDER BY id"
    ).fetchall()
    con.close()
    assert rows[0][0] == d1 and rows[0][1].startswith("use user_version")
    assert rows[1][2] == d1
    # superseding an unknown decision is rejected
    with pytest.raises(ValueError, match="unknown"):
        led.append_decision("proj-continuity", "x", supersedes="nope")


def test_task_transition_history_and_latest_reconstruction(tmp_path):
    led = _ledger(tmp_path)
    led.record_task_transition("proj-continuity", "task-42", "in_progress", note="started")
    led.record_task_transition("proj-continuity", "task-42", "blocked", note="waiting on review")
    led.record_task_transition("proj-continuity", "task-42", "in_progress")
    led.record_task_transition("proj-continuity", "task-42", "done")
    state = led.latest_state("proj-continuity")
    assert state["tasks"]["task-42"]["status"] == "done"
    assert len(state["tasks"]["task-42"]["history"]) == 4
    with pytest.raises(ValueError):
        led.record_task_transition("proj-continuity", "t", "made-up-status")


def test_project_scoping_prevents_cross_project_leakage(tmp_path):
    led = _ledger(tmp_path)
    led.write_checkpoint({**VALID_CHECKPOINT, "project_key": "proj-A"})
    led.write_checkpoint({**VALID_CHECKPOINT, "project_key": "proj-B"})
    led.append_decision("proj-A", "A-only decision")
    led.append_decision("proj-B", "B-only decision")
    led.record_task_transition("proj-B", "task-B", "in_progress")

    sa = led.latest_state("proj-A")
    sb = led.latest_state("proj-B")
    assert sa["checkpoint"]["project_key"] == "proj-A"
    assert [d["content"] for d in sa["decisions"]] == ["A-only decision"]
    assert "task-B" not in sa["tasks"]
    assert sb["checkpoint"]["project_key"] == "proj-B"
    assert [d["content"] for d in sb["decisions"]] == ["B-only decision"]
    assert set(sb["tasks"].keys()) == {"task-B"}
    # unknown project: empty, not another project's data
    assert led.latest_state("proj-does-not-exist") is None


def test_conversation_scoping_filters_state(tmp_path):
    led = _ledger(tmp_path)
    led.write_checkpoint({**VALID_CHECKPOINT, "project_key": "p", "conversation_id": "conv-1"})
    led.write_checkpoint({**VALID_CHECKPOINT, "project_key": "p", "conversation_id": "conv-2"})
    led.append_decision("p", "conv1 decision", conversation_id="conv-1")
    led.append_decision("p", "conv2 decision", conversation_id="conv-2")

    s1 = led.latest_state("p", conversation_id="conv-1")
    assert s1["checkpoint"]["conversation_id"] == "conv-1"
    assert [d["content"] for d in s1["decisions"]] == ["conv1 decision"]
    # unscoped query returns the newest checkpoint across conversations
    s_all = led.latest_state("p")
    assert s_all["checkpoint"]["conversation_id"] == "conv-2"
    assert len(s_all["decisions"]) == 2


def test_checkpoint_rows_are_append_only_across_writes(tmp_path):
    led = _ledger(tmp_path)
    c1 = led.write_checkpoint(VALID_CHECKPOINT)
    c2 = led.write_checkpoint({**VALID_CHECKPOINT, "current_stage": "done-verification"})
    assert c1 != c2
    con = sqlite3.connect(str(led.path))
    n = con.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    con.close()
    assert n == 2
    state = led.latest_state("proj-continuity")
    assert state["checkpoint"]["checkpoint_id"] == c2
    assert state["checkpoint"]["current_stage"] == "done-verification"


def test_status_counts_reports_checkpoints(tmp_path):
    led = _ledger(tmp_path)
    led.write_checkpoint(VALID_CHECKPOINT)
    led.write_checkpoint({**VALID_CHECKPOINT, "status": "done"})
    counts = led.status_counts()
    assert counts == {"in_progress": 1, "done": 1}
