"""Checkpoint tool-surface tests (Task 12).

`context_tuner_checkpoint` accepts structured arguments and returns a
checkpoint ID; `context_tuner_status` shows journal state; the CLI exposes
checkpoint/resume subcommands. Nothing dumps transcript content.
"""
from __future__ import annotations

import json

from hermes_context_tuner.checkpoints import CheckpointLedger
from hermes_context_tuner.engine import ContextTunerEngine

from tests.test_checkpoint_utils import VALID_CHECKPOINT, make_engine


def test_tool_schemas_include_checkpoint_and_resume():
    schemas = ContextTunerEngine.get_tool_schemas(object.__new__(ContextTunerEngine))
    names = {s["name"] for s in schemas}
    assert {"context_tuner_status", "context_tuner_checkpoint", "context_tuner_resume"} <= names
    for s in schemas:
        assert "name" in s and "parameters" in s  # inner schema shape


def test_checkpoint_tool_writes_and_returns_id(tmp_path):
    engine = make_engine(tmp_path)
    raw = engine.handle_tool_call("context_tuner_checkpoint", dict(VALID_CHECKPOINT))
    payload = json.loads(raw)
    assert payload.get("ok") is True
    cid = payload["checkpoint_id"]
    assert isinstance(cid, str) and cid
    state = engine._ledger.latest_state(VALID_CHECKPOINT["project_key"])
    assert state["checkpoint"]["checkpoint_id"] == cid
    assert state["checkpoint"]["goal"] == VALID_CHECKPOINT["goal"]


def test_checkpoint_tool_rejects_invalid_payload_without_writing(tmp_path):
    engine = make_engine(tmp_path)
    bad = dict(VALID_CHECKPOINT)
    bad["status"] = "manifesting"
    raw = engine.handle_tool_call("context_tuner_checkpoint", bad)
    payload = json.loads(raw)
    assert payload.get("ok") is False
    assert "status" in payload.get("error", "")
    assert engine._ledger.latest_state(VALID_CHECKPOINT["project_key"]) is None


def test_checkpoint_tool_failure_is_contained(tmp_path):
    engine = make_engine(tmp_path)
    engine._ledger = None  # degraded: ledger unavailable
    raw = engine.handle_tool_call("context_tuner_checkpoint", dict(VALID_CHECKPOINT))
    payload = json.loads(raw)
    assert payload.get("ok") is False
    assert "unavailable" in payload.get("error", "")


def test_status_tool_still_works_with_ledger_present(tmp_path):
    engine = make_engine(tmp_path)
    engine.handle_tool_call("context_tuner_checkpoint", dict(VALID_CHECKPOINT))
    payload = json.loads(engine.handle_tool_call("context_tuner_status", {}))
    assert payload["status"]["engine"] == "context-tuner"
    assert "journal" in payload["status"]


def test_cli_checkpoint_and_resume_roundtrip(tmp_path, capsys, monkeypatch):
    from hermes_context_tuner import cli

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    payload_file = tmp_path / "cp.json"
    payload_file.write_text(json.dumps(VALID_CHECKPOINT), encoding="utf-8")
    rc = cli.main(["checkpoint", "--file", str(payload_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "checkpoint_id" in out

    rc = cli.main(["resume", "--project", VALID_CHECKPOINT["project_key"]])
    assert rc == 0
    out = capsys.readouterr().out
    assert VALID_CHECKPOINT["goal"] in out
    assert "next_actions" in out


def test_cli_resume_unknown_project_reports_empty(tmp_path, capsys, monkeypatch):
    from hermes_context_tuner import cli

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rc = cli.main(["resume", "--project", "ghost"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no checkpoint state" in out.lower() or '"checkpoint": null' in out


def test_ledger_file_lives_alongside_recovery_store(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    engine = ContextTunerEngine.__new__(ContextTunerEngine)
    engine._delegate = object()  # not used for ledger init
    engine._init_ledger()
    expected = tmp_path / "context_tuner" / "checkpoints.sqlite"
    assert expected.exists()
    assert isinstance(engine._ledger, CheckpointLedger)


def test_checkpoint_tool_records_decisions_and_tasks(tmp_path):
    engine = make_engine(tmp_path)
    args = dict(VALID_CHECKPOINT)
    args["decisions"] = [
        {"content": "use user_version for schema versioning", "source": "review"},
        {"content": "record task transitions"},
    ]
    args["task_updates"] = [
        {"task_id": "task-99", "status": "in_progress", "note": "started"}
    ]
    payload = json.loads(engine.handle_tool_call("context_tuner_checkpoint", args))
    assert payload["ok"] is True
    assert len(payload["decision_ids"]) == 2
    assert payload["tasks_recorded"] == 1
    state = engine._ledger.latest_state(VALID_CHECKPOINT["project_key"])
    assert [d["content"] for d in state["decisions"]] == [
        "use user_version for schema versioning",
        "record task transitions",
    ]
    assert state["tasks"]["task-99"]["status"] == "in_progress"


def test_checkpoint_bundle_is_atomic_on_bad_decision(tmp_path):
    engine = make_engine(tmp_path)
    args = dict(VALID_CHECKPOINT)
    args["decisions"] = [
        {"content": "fine"},
        {"content": "bad", "supersedes": "does-not-exist"},
    ]
    payload = json.loads(engine.handle_tool_call("context_tuner_checkpoint", args))
    assert payload["ok"] is False
    # nothing persisted: no checkpoint, no decision rows
    assert engine._ledger.latest_state(VALID_CHECKPOINT["project_key"]) is None
    assert engine._ledger.status_counts() == {}


def test_checkpoint_bundle_is_atomic_on_bad_task(tmp_path):
    engine = make_engine(tmp_path)
    args = dict(VALID_CHECKPOINT)
    args["task_updates"] = [{"task_id": "t", "status": "made-up"}]
    payload = json.loads(engine.handle_tool_call("context_tuner_checkpoint", args))
    assert payload["ok"] is False
    assert engine._ledger.latest_state(VALID_CHECKPOINT["project_key"]) is None


def test_cli_resume_handles_corrupt_ledger(tmp_path, capsys, monkeypatch):
    from hermes_context_tuner import cli

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    target = tmp_path / "context_tuner"
    target.mkdir(parents=True, exist_ok=True)
    (target / "checkpoints.sqlite").write_bytes(b"this is not a sqlite database")
    rc = cli.main(["resume", "--project", "p"])
    assert rc == 1
    out = capsys.readouterr().out.lower()
    assert '"ok": false' in out
