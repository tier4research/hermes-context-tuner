from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any

from .budget import BudgetPolicy, build_budget_plan
from .checkpoints import CheckpointLedger
from .recovery import RecoveryPointerStore, message_fingerprint

AUDIT_SUMMARY_CAP = 200
RESUME_PACKET_CAP_BYTES = 8_000
STALE_OPERATION_SECONDS = 3_600.0
COMPRESSIONS_BEFORE_CHECKPOINT = 3
MAX_RESUME_KEY_CHARS = 200

# Instance state the wrapper owns. Everything else the host writes onto the
# engine (threshold, probes, telemetry seeds) belongs to the live compressor.
# Landing those writes here is shadow state: reads via __getattr__ still see
# the delegate's old value.
_WRAPPER_LOCAL = frozenset({
    "_delegate",
    "_store",
    "_ledger",
    "_last_plan",
    "_session_id",
    "_pending_operation",
    "_pending_result_fingerprint",
    "_pending_count",
    "_pending_checkpoint_hint",
    "_pending_old_session_id",
    "_budget_policy",
    "_compressions_since_checkpoint",
    "_health",
})


class DelegateCompressionError(RuntimeError):
    """Hermes' compressor raised while executing a valid contract call."""


class DelegateSignatureError(RuntimeError):
    """No candidate call shape matched the delegate's frozen contract.

    Deliberately NOT a TypeError: a signature mismatch must stay
    distinguishable from a TypeError raised *inside* the delegate body, which
    callers classify as a delegate failure rather than a contract mismatch.
    """


def _safe_failure_reason(exc: BaseException, note: str = "") -> str:
    """Content-free, bounded failure reason.

    Exception messages can quote transcript content, credentials, or model
    output, so only the exception class name is ever persisted.
    """
    label = type(exc).__name__ or "Exception"
    if note:
        label = f"{label} {note}"
    return label[:AUDIT_SUMMARY_CAP]


def _fingerprint_messages(messages: list[dict[str, Any]]) -> str:
    """Content-covering, order-sensitive digest of a message list."""
    return message_fingerprint(
        {
            "n": len(messages),
            "fingerprints": [message_fingerprint(m) for m in messages],
        }
    )


def _invoke_compatible(
    fn: Any, candidates: list[tuple[tuple[Any, ...], dict[str, Any]]]
) -> Any:
    """Select a valid frozen contract before invocation; never retry body errors.

    Introspection failures (opaque/C-backed callables) must not refuse the
    call: fall back to the richest candidate so compression is still
    attempted, and let an escaping TypeError be classified as a delegate
    failure by the caller.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        args, kwargs = candidates[0]
        return fn(*args, **kwargs)
    for args, kwargs in candidates:
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return fn(*args, **kwargs)
    raise DelegateSignatureError(f"unsupported Hermes delegate signature: {signature}")


def default_store_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.join(Path.home(), ".hermes")
    return Path(home) / "context_tuner" / "recovery.sqlite"


def _default_ledger_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.join(Path.home(), ".hermes")
    return Path(home) / "context_tuner" / "checkpoints.sqlite"


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
    The journal is an audit sidecar: if any store write fails, compression
    behavior is unchanged (fail-open).
    """

    def __init__(self, budget_policy: BudgetPolicy | None = None):
        self._delegate = _make_default_compressor(model=os.environ.get("HERMES_MODEL") or "unknown")
        # The recovery store is an audit sidecar; if it can't be opened
        # (e.g. sqlite lock contention with concurrent cron sessions),
        # the engine must still load rather than silently falling back
        # to the built-in compressor.
        try:
            self._store = RecoveryPointerStore(default_store_path())
        except Exception:
            self._store = None
        try:
            self._ledger = CheckpointLedger(_default_ledger_path())
        except Exception:
            self._ledger = None
        self._last_plan = None
        self._session_id = ""
        self._pending_operation: str | None = None
        self._pending_result_fingerprint: str | None = None
        self._pending_count = 0
        self._pending_checkpoint_hint = False
        self._pending_old_session_id: str | None = None
        self._budget_policy = budget_policy
        self._compressions_since_checkpoint = 0
        self._health = {"audit_failures": 0, "recovery_failures": 0}
        # Reconcile operations orphaned by process death. Age-gated, so a live
        # in-flight operation owned by another session is never swept.
        self._reconcile_stale_operations()

    def _audit_failure(self) -> None:
        """Count a wrapper-side audit failure without raising.

        Error accounting is wrapper-owned bookkeeping: if it can raise, a
        bookkeeping fault escalates into the host-visible failure this wrapper
        exists to prevent (audit HIGH-3).

        Wrapper-local state is read with ``object.__getattribute__`` because
        ``__getattr__`` forwards unknown reads to the delegate: an unbound
        ``_health`` meant the counter mutated *delegate-owned* state instead of
        being a local no-op (audit MEDIUM delegate-mutation).

        The guarantee is non-throwing for ordinary ``Exception``s under the
        stock class. It does not claim to survive a shadowed helper method or a
        ``BaseException``, which no wrapper-local counter should try to catch.
        """
        try:
            health = object.__getattribute__(self, "_health")
        except Exception:
            return
        try:
            if not isinstance(health, dict):
                return
            health["audit_failures"] = int(health.get("audit_failures", 0) or 0) + 1
        except Exception:
            pass

    def _terminalize_operation(self, operation_id: str | None, reason: str) -> None:
        """Force a phase-1 journal row to a terminal state. Never raises.

        The row is written *before* the delegate runs, so a post-success
        bookkeeping fault must not merely drop the pointer: that leaves the row
        'started' forever and falsifies the compute-then-commit claim
        (audit HIGH-2, HIGH-13). "Interrupted" is the honest state, since the
        compression itself succeeded but its completion could not be recorded.
        """
        try:
            if not operation_id:
                return
            store = object.__getattribute__(self, "_store")
            if store is None:
                return
            store.mark_interrupted_if_started(operation_id, reason=reason[:200])
        except Exception:
            # Never raise, and never assert a terminal state we cannot justify:
            # calling a successful compression 'failed' is provenance
            # corruption in the other direction, and after an uncertain write a
            # second terminal transition could land on a row that is already
            # terminal (audit HIGH-14).
            self._audit_failure()

    def _clear_pending_state(self) -> None:
        """Drop every pending pointer without an interception point.

        ``__setattr__`` can be subclassed or instrumented, and publication is
        incremental: a failure part-way through left a live pointer at a row
        that had just been terminalized, so a later boundary could still act on
        it (audit HIGH-16). Cleanup therefore uses ``object.__setattr__``.
        Never raises.
        """
        try:
            object.__setattr__(self, "_pending_operation", None)
            object.__setattr__(self, "_pending_count", 0)
            object.__setattr__(self, "_pending_result_fingerprint", None)
            object.__setattr__(self, "_pending_checkpoint_hint", False)
            object.__setattr__(self, "_pending_old_session_id", None)
        except Exception:
            self._audit_failure()

    def _reconcile_stale_operations(self) -> None:
        """Close operations left in 'started' by a previous process death."""
        max_age = STALE_OPERATION_SECONDS
        raw = os.environ.get("HERMES_CONTEXT_TUNER_STALE_OP_SECONDS")
        if raw is not None:
            try:
                max_age = float(raw)
            except (TypeError, ValueError):
                max_age = STALE_OPERATION_SECONDS
        if self._store is None:
            return
        try:
            self._store.interrupt_stale_started(max_age_seconds=max_age)
        except Exception:
            self._audit_failure()

    def _checkpoint_refresh_needed(self, current_tokens: int | None) -> bool:
        """Whether repeated lossy compression without an explicit checkpoint
        makes a planned refresh advisable.

        Uses the configured BudgetPolicy when one was supplied; otherwise the
        documented default rule (>= 3 compressions since the last checkpoint).
        """
        if self._budget_policy is not None:
            return self._budget_policy.checkpoint_needed(
                current_tokens=int(current_tokens or 0),
                compressions_since_checkpoint=self._compressions_since_checkpoint,
            )
        return self._compressions_since_checkpoint >= COMPRESSIONS_BEFORE_CHECKPOINT

    @property
    def name(self) -> str:
        return "context-tuner"

    def __getattr__(self, name: str) -> Any:
        """Expose Hermes ContextEngine state fields from the live delegate."""
        # `_delegate` is guarded explicitly: resolving it inside __getattr__
        # would re-enter __getattr__ and recurse forever when the attribute was
        # never assigned.
        if name.startswith("__") or name == "_delegate":
            raise AttributeError(name)
        try:
            return getattr(self._delegate, name)
        except AttributeError as exc:
            raise AttributeError(
                f"{type(self).__name__!s} object has no attribute {name!r}"
            ) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        """Keep wrapper bookkeeping local; forward host mutations to the delegate.

        A write of a name this class defines (``compress``, ``update_model``)
        is an override and must shadow the class method. Every other write,
        once the delegate exists, is the host mutating compressor state.
        """
        if name in _WRAPPER_LOCAL or not hasattr(self, "_delegate"):
            object.__setattr__(self, name, value)
            return
        if name in type(self).__dict__:
            object.__setattr__(self, name, value)
            return
        setattr(self._delegate, name, value)

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: Any = "",
        provider: str = "",
        api_mode: str = "",
        max_tokens: int | None = None,
    ) -> None:
        fn = getattr(self._delegate, "update_model", None)
        if not callable(fn):
            return
        rich = {
            "model": model,
            "context_length": context_length,
            "base_url": base_url,
            "api_key": api_key,
            "provider": provider,
            "api_mode": api_mode,
            "max_tokens": max_tokens,
        }
        narrow = {k: v for k, v in rich.items() if k != "max_tokens"}
        # Bind against the frozen signature first. Do not catch a TypeError
        # from the body and retry — that can run a stateful update twice.
        _invoke_compatible(fn, [((), rich), ((), narrow)])

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
        # ---- phase 0: budget planning (fail-open) ----
        # Planning is wrapper-side work. If it fails, the host must still get
        # the compression it would have gotten without this wrapper: degrade to
        # a passthrough with no plan instead of denying the operation.
        plan = None
        # Safe defaults first: converting caller-supplied tokens is wrapper
        # work and must not run outside the fail-open boundary, or a hostile
        # __int__/__bool__ denies a compression the delegate would have
        # accepted (audit HIGH-4a).
        context_budget = 128_000
        tail_budget = max(8_000, context_budget // 5)
        protected_head = 3
        try:
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
        except Exception:
            # No plan -> no audit journal for this compression, never no
            # compression. Reuses audit_failures to keep the health schema stable.
            self._audit_failure()
            plan = None

        # A previous operation still pending means its boundary callback never
        # arrived (or was lost): it can never finalize and must not be
        # orphaned silently. Record it as interrupted, never as committed.
        if self._pending_operation:
            try:
                self._close_pending_on_teardown(
                    "new compress() began before previous operation's boundary callback"
                )
            except Exception:
                self._audit_failure()
                self._pending_operation = None

        # ---- phase 1: open a 'started' journal operation (fail-open) ----
        pending_op: str | None = None
        lossy = False
        # Plan inspection and trigger construction are wrapper work too, but
        # they are *separate* optional boundaries. Failing to build trigger
        # metadata must not discard an already-valid plan: the journal row is
        # how a compression stays recoverable, and skipping it silently loses
        # the snapshot, the decisions and the provenance -- a hostile
        # focus_topic/force object is not consent to skip journaling
        # (audit HIGH-15, HIGH-4b/4c).
        try:
            lossy = bool(plan is not None and any(
                d.decision in ("summarize", "externalize_hint")
                for d in plan.decisions
            ))
        except Exception:
            self._audit_failure()
            lossy = False
        trigger: dict[str, Any] = {"metadata_degraded": True}
        try:
            trigger = {
                "force": bool(kwargs.get("force", False)),
                # Presence and length only: focus_topic is free text and must
                # never be persisted into the sidecar.
                "focus_topic_present": bool(focus_topic),
                "focus_topic_chars": len(focus_topic) if focus_topic else 0,
                "current_tokens": current_tokens,
            }
        except Exception:
            self._audit_failure()
        try:
            if self._store is None:
                self._store = RecoveryPointerStore(default_store_path())
            # With no plan there is nothing truthful to journal: skip the write
            # rather than record a row with zeroed totals.
            if plan is not None:
                pending_op = self._store.begin_compression(
                    old_session_id=self._session_id,
                    original_count=len(messages),
                    total_tokens=plan.total_tokens,
                    messages=messages,
                    decisions=[d.__dict__ for d in plan.decisions],
                    trigger=trigger,
                    threshold_tokens=context_budget,
                    tail_budget=tail_budget,
                    protected_head=protected_head,
                )
        except Exception:
            self._audit_failure()
            pending_op = None

        # ---- delegate compression (contract preserved) ----
        compress = self._delegate.compress
        try:
            result = _invoke_compatible(
                compress,
                [
                    ((messages,), {"current_tokens": current_tokens, "focus_topic": focus_topic, **kwargs}),
                    ((messages,), {"current_tokens": current_tokens, "focus_topic": focus_topic}),
                    ((messages,), {"current_tokens": current_tokens}),
                    ((messages,), {}),
                ],
            )
        except DelegateSignatureError:
            # Contract mismatch, not a delegate bug: record failed, propagate.
            if pending_op:
                try:
                    self._store.fail_compression(
                        pending_op, reason="unsupported delegate signature"
                    )
                except Exception:
                    self._audit_failure()
            self._pending_operation = None
            raise
        except TypeError as exc:
            # A TypeError raised *by the delegate body* is a real failure and
            # must not be retried with fewer arguments — that masked delegate
            # bugs and could record a phantom operation. Signature mismatch is
            # reported separately as DelegateSignatureError above.
            if pending_op:
                try:
                    self._store.fail_compression(
                        pending_op,
                        reason=_safe_failure_reason(exc, "raised by compression delegate"),
                    )
                except Exception:
                    self._audit_failure()
            self._pending_operation = None
            raise DelegateCompressionError("Hermes compression delegate raised TypeError") from exc
        except Exception as exc:
            # Delegate failure: record failed (content-free reason), re-raise.
            if pending_op:
                try:
                    self._store.fail_compression(
                        pending_op,
                        reason=_safe_failure_reason(exc, "raised by compression delegate"),
                    )
                except Exception:
                    self._audit_failure()
            self._pending_operation = None
            raise

        # ---- phase 2 bookkeeping: wait for host boundary to finalize ----
        # The delegate has already produced a valid result. Wrapper-side
        # bookkeeping must never turn that success into a host-visible
        # exception: a raise here would discard a compression that worked.
        # Compute first, then commit, so a partial failure never leaves a
        # half-registered pending operation behind.
        count = 0
        fingerprint = ""
        hint = False
        try:
            count = len(result) if result is not None else 0
            result_messages = list(result) if isinstance(result, (list, tuple)) else []
            fingerprint = _fingerprint_messages(result_messages)
            # The compression just performed counts toward the refresh cadence,
            # and the rule reads this counter: it must advance *before* the rule
            # is consulted or the cadence is off by one.
            self._compressions_since_checkpoint += 1
            hint = lossy and self._checkpoint_refresh_needed(current_tokens)
        except Exception:
            # The delegate succeeded, so the host still receives the
            # compression. But the phase-1 row already exists: dropping only
            # the in-memory pointer would leave it 'started' forever and make
            # the compute-then-commit claim false (audit HIGH-2/brief HIGH-7).
            # Terminalize it, then degrade. Nothing in this handler may raise.
            try:
                self._audit_failure()
                self._terminalize_operation(
                    pending_op, "post-delegate bookkeeping failed"
                )
            finally:
                # Unconditional: a raising terminalize must not leave the
                # pointer live at a row that is now terminal (audit HIGH-16).
                self._clear_pending_state()
            return result
        try:
            self._pending_operation = pending_op
            self._pending_count = count
            self._pending_result_fingerprint = fingerprint
            self._pending_old_session_id = self._session_id
            self._pending_checkpoint_hint = hint
        except Exception:
            # Publishing the pending pointer is bookkeeping: it must not
            # discard a successful compression, and the row it would have
            # pointed at must not be orphaned (audit HIGH-3, LOW-31).
            try:
                self._audit_failure()
                self._terminalize_operation(
                    pending_op, "pending-state publication failed"
                )
            finally:
                # Partial publication is fully retracted, not just
                # terminalized: a live pointer lets a later boundary finalize
                # a row that is already terminal (audit HIGH-16).
                self._clear_pending_state()
        return result

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        boundary = kwargs.get("boundary_reason") == "compression"
        if self._pending_operation:
            op = self._pending_operation
            expected_old = kwargs.get("old_session_id")
            # Correlate the boundary with the pending operation. Only when both
            # ids are known and differ is this callback for another compression;
            # finalizing then would misattribute the lineage.
            mismatched = (
                boundary
                and bool(expected_old)
                and bool(self._pending_old_session_id)
                and expected_old != self._pending_old_session_id
            )
            if boundary and not mismatched:
                try:
                    self._store.finalize_compression(
                        op,
                        new_session_id=session_id or "",
                        compressed_count=self._pending_count,
                        result_fingerprint=self._pending_result_fingerprint,
                        checkpoint_required=self._pending_checkpoint_hint,
                        checkpoint_reason=(
                            "compression summarized/externalized content; verify an explicit checkpoint exists"
                            if self._pending_checkpoint_hint
                            else None
                        ),
                        conversation_id=kwargs.get("conversation_id"),
                    )
                except Exception as exc:
                    self._audit_failure()
                    # Never leave an operation non-terminal: if the finalize
                    # write failed, close it as interrupted instead.
                    try:
                        self._store.mark_interrupted_if_started(
                            op,
                            reason=_safe_failure_reason(
                                exc, "while finalizing compression boundary"
                            ),
                        )
                    except Exception:
                        self._audit_failure()
            else:
                # Either a non-compression session start, or a boundary whose
                # parent session does not match this operation: it can never be
                # finalized as committed. Record as interrupted, never fake a
                # commit.
                reason = (
                    "boundary parent session did not match the pending operation"
                    if mismatched
                    else "new non-compression session started before boundary callback"
                )
                try:
                    self._store.mark_interrupted_if_started(op, reason=reason)
                except Exception:
                    self._audit_failure()
            self._pending_operation = None
            self._pending_count = 0
            self._pending_result_fingerprint = None
            self._pending_checkpoint_hint = False
            self._pending_old_session_id = None
        self._session_id = session_id or ""
        fn = getattr(self._delegate, "on_session_start", None)
        if callable(fn):
            _invoke_compatible(fn, [((session_id,), kwargs), ((session_id,), {})])

    def _close_pending_on_teardown(self, reason: str) -> None:
        """Mark a still-open operation interrupted (process death / teardown)."""
        op = self._pending_operation
        if not op:
            return
        try:
            if self._store is not None:
                self._store.mark_interrupted_if_started(
                    op, reason=reason[:AUDIT_SUMMARY_CAP]
                )
        except Exception:
            self._audit_failure()
        finally:
            self._pending_operation = None
            self._pending_count = 0
            self._pending_result_fingerprint = None
            self._pending_checkpoint_hint = False
            self._pending_old_session_id = None

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self._close_pending_on_teardown("session ended before compression boundary callback")
        fn = getattr(self._delegate, "on_session_end", None)
        if callable(fn):
            return fn(session_id, messages)

    def on_session_reset(self) -> None:
        self._close_pending_on_teardown("session reset before compression boundary callback")
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
            "health": dict(self._health),
            "journal": self._journal_summary(),
        })
        return status

    def _init_ledger(self) -> None:
        """(Re)initialize the checkpoint ledger; used by tests and after failure."""
        try:
            self._ledger = CheckpointLedger(_default_ledger_path())
        except Exception:
            self._ledger = None

    def _build_resume_packet(
        self,
        project_key: str,
        *,
        conversation_id: str | None = None,
        new_session_id: str = "",
    ) -> dict[str, Any]:
        """Bounded resume packet: latest checkpoint + active decisions +
        unresolved + next actions + artifact refs + recent compression
        pointers. Hard-capped; no transcript content."""
        packet: dict[str, Any] = {
            "project_key": str(project_key)[:MAX_RESUME_KEY_CHARS],
            "conversation_id": (
                str(conversation_id)[:MAX_RESUME_KEY_CHARS]
                if conversation_id is not None
                else None
            ),
            "new_session_id": str(new_session_id)[:MAX_RESUME_KEY_CHARS],
        }
        try:
            if self._ledger is not None:
                state = self._ledger.latest_state(project_key, conversation_id=conversation_id)
            else:
                state = None
        except Exception as exc:
            packet["checkpoint_degraded"] = str(exc)[:AUDIT_SUMMARY_CAP]
            state = None
        if state:
            packet["checkpoint"] = state["checkpoint"]
            packet["decisions"] = [
                {"content": d["content"], "supersedes": d["supersedes"]}
                for d in state["decisions"]
            ]
            packet["tasks"] = {
                t: {"status": v["status"], "history_tail": v["history"][-5:]}
                for t, v in state["tasks"].items()
            }
        else:
            packet["checkpoint"] = None
        try:
            if self._store is not None:
                ev = self._store.latest_events(1)
                if ev:
                    packet["last_compression"] = {
                        k: ev[0].get(k)
                        for k in (
                            "id", "status", "created_at", "old_session_id",
                            "new_session_id", "pre_count", "post_count",
                            "checkpoint_required", "summary_marker",
                        )
                    }
        except Exception as exc:
            packet["journal_degraded"] = str(exc)[:AUDIT_SUMMARY_CAP]
        return self._fit_packet(packet)

    @staticmethod
    def _packet_bytes(packet: dict[str, Any]) -> int:
        return len(json.dumps(packet, ensure_ascii=False, default=str).encode("utf-8"))

    def _fit_packet(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Enforce RESUME_PACKET_CAP_BYTES by shedding in a fixed order.

        The checkpoint payload is capped independently at 16 KB and can be the
        single heaviest section, so it is shed last and progressively: optional
        sections first, then the checkpoint's list fields, then the checkpoint
        reduced to scalar identity. Because every remaining field is bounded
        (MAX_RESUME_KEY_CHARS / MAX_STR_CHARS), the final packet is under cap.
        """
        if self._packet_bytes(packet) <= RESUME_PACKET_CAP_BYTES:
            return packet
        packet["truncated"] = True
        for key in ("tasks", "last_compression", "decisions"):
            packet.pop(key, None)
            if self._packet_bytes(packet) <= RESUME_PACKET_CAP_BYTES:
                return packet
        checkpoint = packet.get("checkpoint")
        if isinstance(checkpoint, dict):
            for key in (
                "completed",
                "locked_decisions",
                "hard_constraints",
                "unresolved",
                "next_actions",
                "artifacts",
                "provenance",
            ):
                checkpoint.pop(key, None)
                if self._packet_bytes(packet) <= RESUME_PACKET_CAP_BYTES:
                    return packet
            packet["checkpoint"] = {
                key: checkpoint[key]
                for key in (
                    "checkpoint_id",
                    "created_at",
                    "project_key",
                    "task_id",
                    "conversation_id",
                    "goal",
                    "current_stage",
                    "status",
                )
                if checkpoint.get(key) is not None
            }
            if self._packet_bytes(packet) <= RESUME_PACKET_CAP_BYTES:
                return packet
            packet["checkpoint"] = {
                key: checkpoint.get(key)
                for key in ("checkpoint_id", "created_at", "status")
                if checkpoint.get(key) is not None
            }
        return packet

    def _journal_summary(self) -> dict[str, Any]:
        try:
            if self._store is None:
                return {"available": False, "degraded": "recovery store unavailable"}
            counts = self._store.status_counts()
            pending = self._store.latest_pending()
            return {
                "available": True,
                "status_counts": counts,
                "pending_operation": pending["operation_id"] if pending else None,
            }
        except Exception as exc:
            return {"available": False, "degraded": str(exc)[:AUDIT_SUMMARY_CAP]}

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
            },
            {
                "name": "context_tuner_checkpoint",
                "description": (
                    "Write an explicit project/task checkpoint (structured state that"
                    " survives context compression). Requires project_key, goal,"
                    " current_stage, status; optional task_id, conversation_id,"
                    " completed, locked_decisions, hard_constraints, unresolved,"
                    " next_actions, artifacts, provenance, decisions"
                    " (with optional supersedes), task_updates."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project_key": {"type": "string"},
                        "task_id": {"type": "string"},
                        "conversation_id": {"type": "string"},
                        "goal": {"type": "string"},
                        "current_stage": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["in_progress", "blocked", "done", "abandoned"],
                        },
                        "completed": {"type": "array", "items": {"type": "string"}},
                        "locked_decisions": {"type": "array", "items": {"type": "string"}},
                        "hard_constraints": {"type": "array", "items": {"type": "string"}},
                        "unresolved": {"type": "array", "items": {"type": "string"}},
                        "next_actions": {"type": "array", "items": {"type": "string"}},
                        "artifacts": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "sha256": {"type": "string"},
                                },
                                "required": ["path"],
                            },
                        },
                        "provenance": {"type": "string"},
                        "decisions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {"type": "string"},
                                    "supersedes": {"type": "string"},
                                    "source": {"type": "string"},
                                },
                                "required": ["content"],
                            },
                        },
                        "task_updates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "task_id": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": [
                                            "in_progress",
                                            "blocked",
                                            "done",
                                            "abandoned",
                                        ],
                                    },
                                    "note": {"type": "string"},
                                },
                                "required": ["task_id", "status"],
                            },
                        },
                    },
                    "required": ["project_key", "goal", "current_stage", "status"],
                },
            },
            {
                "name": "context_tuner_resume",
                "description": (
                    "Retrieve the bounded resume packet for a project/conversation:"
                    " latest checkpoint, active decisions, unresolved items, next"
                    " actions, artifact references. Call at session start after a"
                    " compression or refresh."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project_key": {"type": "string"},
                        "conversation_id": {"type": "string"},
                        "new_session_id": {"type": "string"},
                    },
                    "required": ["project_key"],
                },
            },
        ]

    def handle_tool_call(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if name == "context_tuner_status":
            try:
                limit = int(args.get("limit", 5) if isinstance(args, dict) else 5)
            except (TypeError, ValueError):
                limit = 5
            try:
                events = self._store.latest_events(limit) if self._store else []
            except Exception as exc:
                events = [{"error": f"recovery store unavailable: {exc}"}]
            try:
                status = self.get_status()
            except Exception as exc:
                status = {"engine": self.name, "degraded": f"status unavailable: {exc}"}
            return json.dumps({"status": status, "events": events}, default=str)
        if name == "context_tuner_checkpoint":
            return self._handle_checkpoint_tool(args if isinstance(args, dict) else {})
        if name == "context_tuner_resume":
            return self._handle_resume_tool(args if isinstance(args, dict) else {})
        return json.dumps({"error": f"unknown context tuner tool: {name}"})

    def _handle_checkpoint_tool(self, args: dict[str, Any]) -> str:
        try:
            if self._ledger is None:
                # degraded: do NOT silently re-create a ledger that failed at
                # init; report unavailability so the caller can decide.
                return json.dumps({"ok": False, "error": "checkpoint ledger unavailable"})
            decisions = args.get("decisions") or []
            task_updates = args.get("task_updates") or []
            if not isinstance(decisions, list) or not isinstance(task_updates, list):
                return json.dumps(
                    {"ok": False, "error": "decisions and task_updates must be lists"}
                )
            result = self._ledger.write_checkpoint_bundle(
                args, decisions=decisions, task_updates=task_updates
            )
            # An explicit checkpoint resets the lossy-compression counter.
            self._compressions_since_checkpoint = 0
            return json.dumps(
                {
                    "ok": True,
                    "checkpoint_id": result["checkpoint_id"],
                    "decision_ids": result["decision_ids"],
                    "tasks_recorded": result["tasks_recorded"],
                }
            )
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"checkpoint write failed: {exc}"})

    def _handle_resume_tool(self, args: dict[str, Any]) -> str:
        project_key = str(args.get("project_key") or "").strip()
        if not project_key:
            return json.dumps({"ok": False, "error": "project_key is required"})
        if len(project_key) > MAX_RESUME_KEY_CHARS:
            return json.dumps({"ok": False, "error": "project_key is too long"})
        try:
            packet = self._build_resume_packet(
                project_key,
                conversation_id=args.get("conversation_id"),
                new_session_id=str(args.get("new_session_id") or ""),
            )
            return json.dumps({"ok": True, "packet": packet}, default=str)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"resume failed: {exc}"})


def context_tuner_command(args: str = "", **kwargs: Any) -> str:
    try:
        store = RecoveryPointerStore(default_store_path())
        events = store.latest_events(10)
        counts = store.status_counts()
    except Exception as exc:
        return f"Hermes Context Tuner: recovery store unavailable ({exc})."
    if not events:
        return "Hermes Context Tuner is installed. No compression events recorded yet."
    lines = ["Hermes Context Tuner recent compression events:"]
    for ev in events:
        status = ev.get("status") or "unknown"
        marker = " *CHECKPOINT*" if ev.get("checkpoint_required") else ""
        lines.append(
            f"- #{ev['id']} [{status}] messages {ev['original_count']} -> {ev['compressed_count']}; "
            f"~{ev['total_tokens']} tokens; {ev['summary']}{marker}"
        )
    lines.append(f"journal totals: {counts}")
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
