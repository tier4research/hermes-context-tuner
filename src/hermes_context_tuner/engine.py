from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any

from .budget import build_budget_plan
from .recovery import RecoveryPointerStore


def default_store_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.join(Path.home(), ".hermes")
    return Path(home) / "context_tuner" / "recovery.sqlite"


def _make_default_compressor(**kwargs: Any):
    """Instantiate Hermes' current built-in compressor with best-effort kwargs.

    This is the compatibility seam: Hermes has changed ContextCompressor's
    constructor over time. We inspect the live signature and pass only supported
    parameters, so the engine survives normal Hermes upgrades.
    """
    from agent.context_compressor import ContextCompressor

    sig = inspect.signature(ContextCompressor)
    allowed = {k: v for k, v in kwargs.items() if k in sig.parameters}
    if "model" in sig.parameters and "model" not in allowed:
        allowed["model"] = kwargs.get("model") or os.environ.get("HERMES_MODEL") or "unknown"
    return ContextCompressor(**allowed)


class ContextTunerEngine:
    """Hermes ContextEngine wrapper that delegates compression to built-in Hermes.

    It does not fork or monkey-patch Hermes. It implements the official
    ContextEngine ABC shape and is installed as `plugins/context_engine/context-tuner`.
    If Hermes updates the built-in compressor internals, this wrapper continues
    to work as long as the ContextEngine contract remains stable. If unsupported
    constructor parameters change, `_make_default_compressor` filters them.
    """

    def __init__(self):
        self._delegate = _make_default_compressor(model=os.environ.get("HERMES_MODEL") or "unknown")
        # The recovery store is an audit sidecar; if it can't be opened
        # (e.g. sqlite lock contention with concurrent cron sessions),
        # the engine must still load rather than silently falling back
        # to the built-in compressor.
        try:
            self._store = RecoveryPointerStore(default_store_path())
        except Exception:
            self._store = None
        self._last_plan = None
        self._session_id = ""

    @property
    def name(self) -> str:
        return "context-tuner"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def update_model(self, model: str, context_length: int, base_url: str = "", api_key: Any = "", provider: str = "", api_mode: str = "") -> None:
        if hasattr(self._delegate, "update_model"):
            self._delegate.update_model(model, context_length, base_url=base_url, api_key=api_key, provider=provider, api_mode=api_mode)

    def update_from_response(self, usage: dict[str, Any]) -> None:
        return self._delegate.update_from_response(usage)

    def should_compress(self, prompt_tokens: int = None) -> bool:
        return self._delegate.should_compress(prompt_tokens)

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        fn = getattr(self._delegate, "should_defer_preflight_to_real_usage", None)
        return bool(fn(rough_tokens)) if callable(fn) else False

    def has_content_to_compress(self, messages: list[dict[str, Any]]) -> bool:
        fn = getattr(self._delegate, "has_content_to_compress", None)
        return bool(fn(messages)) if callable(fn) else True

    def compress(self, messages: list[dict[str, Any]], current_tokens: int = None, focus_topic: str = None, **kwargs: Any) -> list[dict[str, Any]]:
        context_budget = int(getattr(self._delegate, "threshold_tokens", 0) or current_tokens or 0 or 128_000)
        tail_budget = int(getattr(self._delegate, "tail_token_budget", 0) or max(8_000, context_budget // 5))
        protected_head = int(getattr(self._delegate, "protect_first_n", 3) or 3)
        plan = build_budget_plan(
            messages,
            context_budget=context_budget,
            tail_budget=tail_budget,
            protected_head=protected_head,
        )
        self._last_plan = plan

        compress = getattr(self._delegate, "compress")
        try:
            result = compress(messages, current_tokens=current_tokens, focus_topic=focus_topic, **kwargs)
        except TypeError:
            try:
                result = compress(messages, current_tokens=current_tokens, focus_topic=focus_topic)
            except TypeError:
                result = compress(messages, current_tokens=current_tokens)

        try:
            if self._store is None:
                self._store = RecoveryPointerStore(default_store_path())
            self._store.record_event(
                old_session_id=self._session_id,
                new_session_id=self._session_id,
                original_count=len(messages),
                compressed_count=len(result),
                total_tokens=plan.total_tokens,
                decisions=[d.__dict__ for d in plan.decisions],
            )
        except Exception:
            # Audit failure must never break compression.
            pass
        return result

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id or ""
        fn = getattr(self._delegate, "on_session_start", None)
        if callable(fn):
            try:
                fn(session_id, **kwargs)
            except TypeError:
                fn(session_id)

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        fn = getattr(self._delegate, "on_session_end", None)
        if callable(fn):
            return fn(session_id, messages)

    def on_session_reset(self) -> None:
        fn = getattr(self._delegate, "on_session_reset", None)
        if callable(fn):
            return fn()

    def get_status(self) -> dict[str, Any]:
        fn = getattr(self._delegate, "get_status", None)
        status = dict(fn()) if callable(fn) else {}
        status.update({
            "engine": self.name,
            "recovery_store": str(default_store_path()),
            "last_plan": self._last_plan.to_dict() if self._last_plan else None,
        })
        return status

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        # Hermes ContextEngine.get_tool_schemas() returns the *inner* function
        # schema. agent_init wraps each entry as {"type": "function", "function": schema}.
        # Returning an already OpenAI-wrapped schema here creates a malformed
        # double-wrap (`tools[n].function` has no `name`), which strict
        # providers such as opencode-go / DeepSeek reject.
        return [
            {
                "name": "context_tuner_status",
                "description": "Show Hermes Context Tuner compression/audit status and recent recovery pointers.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 5}},
                },
            }
        ]

    def handle_tool_call(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if name != "context_tuner_status":
            return json.dumps({"error": f"unknown context tuner tool: {name}"})
        limit = int(args.get("limit", 5) if isinstance(args, dict) else 5)
        try:
            events = self._store.latest_events(limit) if self._store else []
        except Exception as exc:
            events = [{"error": f"recovery store unavailable: {exc}"}]
        return json.dumps({"status": self.get_status(), "events": events}, default=str)


def context_tuner_command(args: str = "", **kwargs: Any) -> str:
    try:
        store = RecoveryPointerStore(default_store_path())
        events = store.latest_events(10)
    except Exception as exc:
        return f"Hermes Context Tuner: recovery store unavailable ({exc})."
    if not events:
        return "Hermes Context Tuner is installed. No compression events recorded yet."
    lines = ["Hermes Context Tuner recent compression events:"]
    for ev in events:
        lines.append(
            f"- #{ev['id']} messages {ev['original_count']} -> {ev['compressed_count']}; "
            f"~{ev['total_tokens']} tokens; {ev['summary']}"
        )
    return "\n".join(lines)


def register(ctx: Any) -> None:
    ctx.register_context_engine(ContextTunerEngine())
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            "context-tuner",
            context_tuner_command,
            description="Show Hermes Context Tuner recovery/audit status.",
            args_hint="",
        )
