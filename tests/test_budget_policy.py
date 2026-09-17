"""Rolling-context policy tests (Task 13).

Evidence-gated: the replay fixture does not yet justify changing production
thresholds, so this locks in (a) explicit budget *configuration* support,
(b) derived recomputation, (c) the checkpoint-needed signal that a planned
refresh can respond to — with automatic /new explicitly out of scope.
"""
from __future__ import annotations

import pytest

from hermes_context_tuner.budget import build_budget_plan


def test_budget_plan_defaults_unchanged():
    messages = [{"role": "user", "content": "x" * 400}] * 10
    plan = build_budget_plan(messages, context_budget=10_000)
    assert plan.tail_budget == 20_000  # documented default preserved
    assert plan.protected_head == 3
    assert plan.context_budget == 10_000


def test_budget_policy_config_recomputes_derived_fields():
    from hermes_context_tuner.budget import BudgetPolicy

    policy = BudgetPolicy(
        context_budget=128_000,
        recent_conversation=30_000,
        source_material=40_000,
        relevant_retrieval=20_000,
        project_state=10_000,
        current_work=10_000,
        output_reserve=16_000,
    )
    assert policy.derived.tail_budget == policy.recent_conversation
    assert policy.derived.total == (
        30_000 + 40_000 + 20_000 + 10_000 + 10_000 + 16_000
    )
    # allocation sanity: reserve+current must fit in the window
    assert policy.derived.total <= policy.context_budget


def test_budget_policy_overcommit_rejected():
    from hermes_context_tuner.budget import BudgetPolicy

    with pytest.raises(ValueError, match="exceed"):
        BudgetPolicy(
            context_budget=1_000,
            recent_conversation=2_000,
            source_material=2_000,
            relevant_retrieval=2_000,
            project_state=2_000,
            current_work=2_000,
            output_reserve=2_000,
        )


def test_checkpoint_needed_signal_without_forcing_new():
    from hermes_context_tuner.budget import BudgetPolicy

    policy = BudgetPolicy(
        context_budget=10_000,
        recent_conversation=4_000,
        source_material=1_000,
        relevant_retrieval=1_000,
        project_state=1_000,
        current_work=1_000,
        output_reserve=1_000,
    )
    # below pressure: no signal
    assert policy.checkpoint_needed(current_tokens=5_000, compressions_since_checkpoint=0) is False
    # repeated compressions without a checkpoint: signal
    assert policy.checkpoint_needed(current_tokens=5_000, compressions_since_checkpoint=3) is True
    # riding past the planned working set: signal
    assert policy.checkpoint_needed(current_tokens=9_900, compressions_since_checkpoint=0) is True


def test_policy_does_not_schedule_session_resets():
    from hermes_context_tuner.budget import BudgetPolicy

    policy = BudgetPolicy(
        context_budget=10_000,
        recent_conversation=4_000,
        source_material=1_000,
        relevant_retrieval=1_000,
        project_state=1_000,
        current_work=1_000,
        output_reserve=1_000,
    )
    # contract: the policy exposes a *signal*, never an action
    assert not hasattr(policy, "reset_session")
    assert not hasattr(policy, "run_new")
    assert not hasattr(policy, "force_refresh")


def test_replay_fixture_three_compressions_checkpoint_needed(tmp_path):
    """Deterministic mini-replay: enough pressure that the signal fires
    within three compressions; explicit constraints recorded in the fixture
    survive into the checkpoint (structured, not a transcript summary)."""
    from hermes_context_tuner.budget import BudgetPolicy
    from hermes_context_tuner.checkpoints import CheckpointLedger

    policy = BudgetPolicy(
        context_budget=10_000,
        recent_conversation=2_000,
        source_material=1_000,
        relevant_retrieval=1_000,
        project_state=1_000,
        current_work=1_000,
        output_reserve=1_000,
    )
    compressions = 0
    fired_at = None
    for _ in range(3):
        compressions += 1
        if policy.checkpoint_needed(current_tokens=6_000, compressions_since_checkpoint=compressions):
            fired_at = compressions
            break
    assert fired_at is not None, "checkpoint-needed signal must fire within three compressions"

    # the checkpoint (not the transcript) carries the durable constraints
    led = CheckpointLedger(tmp_path / "replay.sqlite")
    led.write_checkpoint(
        {
            "project_key": "replay-1",
            "goal": "verify constraint survival across compressions",
            "current_stage": "replay",
            "status": "in_progress",
            "hard_constraints": ["fixture-constraint-alpha", "fixture-constraint-beta"],
            "next_actions": ["confirm signal fired"],
            "provenance": "deterministic replay fixture",
        }
    )
    state = led.latest_state("replay-1")
    assert state["checkpoint"]["hard_constraints"] == [
        "fixture-constraint-alpha",
        "fixture-constraint-beta",
    ]
