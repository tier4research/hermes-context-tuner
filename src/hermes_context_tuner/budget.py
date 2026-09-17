from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from typing import Any, Iterable

_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class BudgetPolicy:
    """Configurable rolling-context allocation (Task 13).

    Evidence-gated: production defaults are NOT changed by this class's
    existence — it only becomes authoritative when a config explicitly
    supplies it. The checkpoint_needed signal requests a *planned* refresh;
    it never schedules or forces a session reset.
    """

    context_budget: int
    recent_conversation: int
    source_material: int
    relevant_retrieval: int
    project_state: int
    current_work: int
    output_reserve: int

    @dataclass(frozen=True)
    class Derived:
        tail_budget: int
        total: int

    @property
    def derived(self) -> "BudgetPolicy.Derived":
        total = (
            self.recent_conversation
            + self.source_material
            + self.relevant_retrieval
            + self.project_state
            + self.current_work
            + self.output_reserve
        )
        return BudgetPolicy.Derived(tail_budget=self.recent_conversation, total=total)

    def __post_init__(self) -> None:
        if self.derived.total > self.context_budget:
            raise ValueError(
                f"budget allocation ({self.derived.total}) exceeds context_budget"
                f" ({self.context_budget})"
            )
        for name in (
            "context_budget", "recent_conversation", "source_material",
            "relevant_retrieval", "project_state", "current_work", "output_reserve",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"budget field '{name}' must be non-negative")

    def checkpoint_needed(
        self, *, current_tokens: int, compressions_since_checkpoint: int
    ) -> bool:
        """Bounded signal that a planned refresh/checkpoint is advisable.

        Fires on either: riding past the planned working set, or repeated
        compressions without an explicit checkpoint (lossy-summary risk).
        """
        working_set = self.derived.total - self.output_reserve
        if current_tokens > int(0.9 * working_set):
            return True
        return compressions_since_checkpoint >= 3


@dataclass(frozen=True)
class MessageDecision:
    index: int
    role: str
    rough_tokens: int
    decision: str  # keep_raw | summarize | externalize_hint
    reason: str


@dataclass(frozen=True)
class BudgetPlan:
    total_tokens: int
    context_budget: int
    tail_budget: int
    protected_head: int
    decisions: tuple[MessageDecision, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["decisions"] = [asdict(d) for d in self.decisions]
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content_text(v) for v in value)
    if isinstance(value, dict):
        # Prefer textual leaves without dumping huge binary/multimodal payloads.
        parts: list[str] = []
        for key in ("text", "content", "summary", "output", "result"):
            if key in value:
                parts.append(_content_text(value.get(key)))
        return "\n".join(p for p in parts if p)
    return str(value)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    text = _content_text(message.get("content"))
    tokens = max(1, len(text) // _CHARS_PER_TOKEN + 8)
    for tool_call in message.get("tool_calls") or []:
        try:
            fn = tool_call.get("function", {}) if isinstance(tool_call, dict) else getattr(tool_call, "function", None)
            args = fn.get("arguments", "") if isinstance(fn, dict) else getattr(fn, "arguments", "")
            tokens += len(str(args)) // _CHARS_PER_TOKEN
        except Exception:
            tokens += 16
    return tokens


def build_budget_plan(
    messages: Iterable[dict[str, Any]],
    *,
    context_budget: int,
    tail_budget: int = 20_000,
    protected_head: int = 3,
    externalize_tool_threshold: int = 2_000,
) -> BudgetPlan:
    """Build an advisory context packing plan.

    This is deliberately non-mutating. Hermes Context Tuner uses it to audit and
    record what the engine *would* prefer to keep raw/externalize/summarize,
    while the active context engine delegates actual compression to Hermes'
    built-in compressor unless Hermes exposes richer hooks.
    """
    items = list(messages)
    token_counts = [estimate_message_tokens(m) for m in items]
    total = sum(token_counts)

    keep_tail: set[int] = set()
    acc = 0
    min_tail_messages = min(1, len(items))
    for idx in range(len(items) - 1, -1, -1):
        protected_count = len(keep_tail)
        would_exceed = acc > 0 and (acc + token_counts[idx]) > tail_budget
        if protected_count >= min_tail_messages and would_exceed:
            break
        keep_tail.add(idx)
        acc += token_counts[idx]

    decisions: list[MessageDecision] = []
    for idx, (msg, toks) in enumerate(zip(items, token_counts)):
        role = str(msg.get("role", ""))
        if idx < protected_head:
            decision = "keep_raw"
            reason = "protected head"
        elif idx in keep_tail:
            decision = "keep_raw"
            reason = "protected recent tail"
        elif role == "tool" and toks >= externalize_tool_threshold:
            decision = "externalize_hint"
            reason = f"large old tool output (~{toks} tokens)"
        else:
            decision = "summarize"
            reason = "middle context"
        decisions.append(MessageDecision(idx, role, toks, decision, reason))

    return BudgetPlan(
        total_tokens=total,
        context_budget=context_budget,
        tail_budget=tail_budget,
        protected_head=protected_head,
        decisions=tuple(decisions),
    )
