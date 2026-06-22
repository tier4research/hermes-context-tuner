"""Hermes Context Tuner.

A small, upgrade-resilient context-engine wrapper for Hermes Agent. It delegates
compression to Hermes' built-in ContextCompressor and adds budget planning,
context audit metadata, and recovery-pointer sidecar records.
"""

from .budget import BudgetPlan, MessageDecision, build_budget_plan, estimate_message_tokens
from .recovery import RecoveryPointerStore
from .engine import DelegateCompressionError

__version__ = "0.1.0-alpha.1"

__all__ = [
    "BudgetPlan",
    "MessageDecision",
    "RecoveryPointerStore",
    "build_budget_plan",
    "estimate_message_tokens",
    "DelegateCompressionError",
]
