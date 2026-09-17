# v0.3.0

Context continuity journal: compression lineage you can trust, plus explicit
project checkpoints that survive compression and session refreshes.

## New

- **Two-phase compression journal (schema v3).** Every compression opens a
  `started` operation and finalizes it only when Hermes reports the committed
  compression boundary (`on_session_start(boundary_reason="compression")`).
  Statuses: `started / finalized / failed / aborted / interrupted`. A
  compressor return value alone is never reported as committed. Idempotent
  finalization; duplicate callbacks cannot double-commit.
- **Additive, idempotent migration.** Existing v1/v2 sidecar DBs (including
  live DBs with `operation_id/status/finalized_at` ALTERed on) migrate in
  place without losing events or pointers; `PRAGMA user_version` carries the
  version; unsupported future versions fail clearly instead of rewriting.
- **Pointer/fingerprint accounting.** Per-message ranges, roles, token
  estimates, decisions, and content fingerprints — enough to verify what was
  kept, summarized, or externalized. No message bodies ever touch the
  sidecar (tested, including WAL files).
- **Explicit checkpoint ledger** (`checkpoints.sqlite`, append-only):
  structured project/task state — goal, stage, completed, locked decisions,
  hard constraints, unresolved, next actions, artifact refs — with bounded
  payloads, atomic validation rejections, and supersession-based corrections.
  `context_tuner_checkpoint` also accepts `decisions` (with an optional
  `supersedes` link) and `task_updates`; the checkpoint, its decision events,
  and its task transitions are written in one transaction, so a rejected entry
  persists nothing at all.
- **Bounded resume packet.** `context_tuner_resume` (agent tool) and
  `hermes-context-tuner resume` (CLI) return the latest checkpoint, active
  decisions, task state, and last compression pointer, hard-capped at 8 KB.
  The cap is enforced by ordered shedding (optional sections → checkpoint list
  fields → checkpoint scalars) with the envelope keys bounded too, so it holds
  even for a maximal 16 KB checkpoint. `context_tuner_checkpoint` (tool) and
  `checkpoint --file` (CLI) write them.
- **`BudgetPolicy` (opt-in).** Configurable rolling-context allocation with a
  `checkpoint_needed()` signal for planned refreshes. Pass one to the engine as
  `ContextTunerEngine(budget_policy=...)` and it drives the journal's
  `checkpoint_required` flag; without one, the documented default rule applies
  (≥ 3 compressions since the last explicit checkpoint). Production defaults
  are otherwise unchanged; no automatic `/new`, no automatic session resets.

## Behavior & safety

- Compression behavior is unchanged: the journal is a fail-open audit
  sidecar. Store failures are visible in `context_tuner_status` health/journal
  fields and never break or alter compression.
- Status views (`context_tuner_status`, `/context-tuner`) distinguish all
  lifecycle states and agree with direct DB queries; the watchdog's
  `status='finalized'` query is locked by tests and remains compatible.

## Hardening (independent adversarial review)

Findings from an independent source-level review, fixed and regression-tested:

- **Privacy:** a delegate failure records only the exception *class name*;
  exception messages (which can quote transcript content or credentials) are
  never persisted. `focus_topic` is stored as a presence flag and length, not
  text. Verified against the DB and its WAL/SHM sidecars.
- **Lifecycle integrity:** a failed finalize now closes the operation as
  `interrupted` instead of leaving it `started` forever, and operations
  orphaned by process death are reconciled at engine startup by an age-gated
  sweep (`HERMES_CONTEXT_TUNER_STALE_OP_SECONDS`, default 1 hour) that never
  touches a live in-flight operation.
- **Boundary correlation:** the committed-boundary callback is attributed to
  the pending operation only when its parent session matches; a mismatch marks
  the operation `interrupted` rather than misattributing lineage.
- **Delegate errors:** a signature mismatch is reported as
  `DelegateSignatureError` (distinct from a body `TypeError`), and a delegate
  whose signature cannot be introspected is still called rather than refusing
  compression.
- **Tool robustness:** the status tool tolerates malformed arguments and a
  failing delegate `get_status()`; the resume CLI returns a JSON error envelope
  rather than a traceback.

## Rollback

Stop Hermes and restore the pre-migration backup of `recovery.sqlite`
(taken via SQLite's backup API before upgrading). There is no destructive
down-migration.

---

# v0.2.0-beta.1

Context compression audit, recovery pointers, and agent-operable context hygiene — stable beta.

## What's new since alpha

- **Sales-forward README.** Rewritten to lead with the problem (invisible compression, no recovery path, blind token pressure) and highlight what makes it different: budget-aware packing plans, compression audit trail, agent-operable commands, and upgrade resilience.
- **Stability improvements.** Recovery store hardened against SQLite lock contention from concurrent cron sessions. Context engine tool schema aligned with Hermes' `agent_init` contract (no double-wrapped function schemas).
- **Production data available.** 78 real compression events recorded across daily use, 10,565 recovery pointers — decision distribution: `keep_raw=6789`, `summarize=3592`, `externalize_hint=184`.

## Known limitations

- No CI matrix against multiple Hermes versions yet — currently tested against Hermes main.
- Externalization hints for large tool outputs are advisory only; actual pruning depends on the Hermes built-in compressor.
- Recovery pointer store is a sidecar SQLite file, not yet integrated with Hermes' session DB.

---

# v0.1.0-alpha.1

Initial public alpha of Hermes Context Tuner.

## What it is

Hermes Context Tuner is an upgrade-resilient wrapper around Hermes Agent's built-in context compressor. It keeps standard Hermes compression, but adds the operational pieces agents need during long sessions:

- budget-aware context pack plans
- compression/recovery audit metadata
- `context_tuner_status` context-engine tool
- `/context-tuner` command
- `optimize-context` skill example
- reinstallable Hermes context-engine shim

## Included

- `hermes_context_tuner.budget` advisory budget-plan library
- `RecoveryPointerStore` SQLite sidecar for compression audit/recovery metadata
- `ContextTunerEngine`, an official Hermes `ContextEngine` wrapper that delegates to the live built-in compressor
- `hermes-context-tuner install-hermes --set-config` installer for the Hermes context-engine shim
- `examples/skills/optimize-context/SKILL.md`
- integration and architecture docs

## Upgrade posture

The package does not patch Hermes core or monkey-patch private methods. It uses the official context-engine seam and can be reinstalled after Hermes updates by rerunning:

```bash
hermes-context-tuner install-hermes --set-config
```
