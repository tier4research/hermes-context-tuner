"""Hermes Context Tuner.

A small, upgrade-resilient context-engine wrapper for Hermes Agent. It delegates
compression to Hermes' built-in ContextCompressor and adds budget planning,
context audit metadata, and recovery-pointer sidecar records.
"""

from .budget import BudgetPlan, MessageDecision, build_budget_plan, estimate_message_tokens
from .engine import ContextTunerEngine, context_tuner_command, register
from .recovery import RecoveryPointerStore

__version__ = "0.3.3"

__all__ = [
    "BudgetPlan",
    "ContextTunerEngine",
    "MessageDecision",
    "RecoveryPointerStore",
    "build_budget_plan",
    "context_tuner_command",
    "estimate_message_tokens",
    "register",
]
