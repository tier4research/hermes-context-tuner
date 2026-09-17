"""Shared helpers for checkpoint tool tests."""
from __future__ import annotations

from pathlib import Path

from hermes_context_tuner.checkpoints import CheckpointLedger
from hermes_context_tuner.recovery import RecoveryPointerStore

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


def make_engine(tmp_path: Path):
    """Build a ContextTunerEngine via __new__ with fake delegate + temp stores."""
    from tests.test_engine_journal import FakeDelegate, make_engine as base_make

    engine = base_make(tmp_path, FakeDelegate())
    engine._ledger = CheckpointLedger(tmp_path / "cp.sqlite")
    return engine
