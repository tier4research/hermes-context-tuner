# v0.3.3

Verification honesty, second pass. Compression behaviour is unchanged again;
what changed is that the installer's *proof* is now loadability rather than
file placement, and that recovery bookkeeping can no longer damage state it
does not own.

## Fixed

- **The installer now loads the engine instead of locating it.** 0.3.2's probe
  asked the loader to resolve a directory and called that verified, so a shim
  that was empty or raised on import still verified as installed. The probe now
  exercises the host's own activation path (`load_context_engine`) and requires
  a real engine object back; the bundled-shadow check, discovery check and
  framed-result check all report which one failed.
- **A tree with no Hermes runtime is "not verified", not a failed install.**
  Copying the shim is the whole operation when Hermes is not there yet; the old
  fail-closed exit code turned an ordinary pre-Hermes install into an error.
  The exit code stays strict for the live Hermes home and advisory for an
  explicit `--hermes-repo` build target -- so the CI job that checks the host
  contract is not failed by the host it is checking.
- **A failed `--set-config` is now fatal instead of silent.** `hermes config
  set` ran with `check=False` inside the copy step and could fail with the
  installer still printing success: the engine was activated in name only.
- **Post-success recovery no longer records a delivered compression as
  failed.** Terminalizing a phase-1 row uses `mark_interrupted_if_started`,
  never `fail_compression`: "the work happened, its completion record did not"
  is `interrupted`, and calling it failed is provenance corruption in the
  other direction.
- **Error accounting can no longer mutate the delegate.** `_audit_failure`
  reads its own health dict with `object.__getattribute__`, because an unbound
  `_health` would otherwise be forwarded to the delegate by `__getattr__` and
  have the host's own dictionary incremented.
- **Recovery cleanup runs in a `finally`.** A raising terminalize can no
  longer leave a live pending pointer aimed at a row that is already terminal.
- **A hostile `focus_topic` no longer costs the compression its journal row.**
  Plan inspection and trigger-metadata construction are inside their own
  guarded boundaries, so a malformed argument degrades the *metadata* (marked
  `metadata_degraded`) instead of discarding an already-valid plan or skipping
  journaling.

## Tests

142 passing (was 131). The 16 new/extended installer- and terminal-state tests
were RED-proven against `821d5f1`: 12 fail there, including an empty shim, an
import-raising shim, an engine missing from discovery, a silent probe, a
pre-Hermes tree, a shadowed checkout, and the delegate-mutation case.

## Known open items

- F11 (open): boundary correlation is permissive when session ids are absent,
  and the concurrency claim there is asserted rather than tested.
- Verification is point-in-time: it imports the module tree now; a later
  Hermes upgrade can invalidate it. Re-run `install-hermes` after upgrades.

# v0.3.2

Installer verification and journal-row honesty. Compression behaviour is
unchanged; both defects were in what the wrapper -- and the installer --
*claimed* about work they had not actually verified.

## Fixed

- **The installer no longer reports success it has not verified.** Hermes
  resolves engine shims bundled-first, so a stale or empty bundled directory
  shadows the user install while `install-hermes` still printed "installed".
  The CLI now probes the real loader after copying and exits non-zero, naming
  the reason, whenever the engine will not load (audit CRITICAL-1: a
  false-success deployment path).
- **No journal row is left `started` forever.** The phase-1 row is written
  *before* the delegate runs, so a post-success bookkeeping fault must
  terminalize that row instead of dropping only the in-memory pointer. The
  earlier claim that "no half-registered pending operation is left behind" was
  false (audit HIGH-2, HIGH-13).
- **Error accounting can no longer escalate into a host-visible failure.** Every
  `health["audit_failures"]` increment is replaced by a non-throwing
  `_audit_failure()` helper, and plan inspection, trigger construction and
  pending-state publication now sit inside the fail-open boundary (audit
  HIGH-3, HIGH-4a/b/c).

## Registered, not fixed

The remaining findings are registered in the findings registry rather than
silently deferred: permissive boundary correlation when session ids are absent
(HIGH-9), delegate-result corruption reported as a bookkeeping failure (HIGH-7),
and unmodelled operation concurrency (HIGH-8). Each needs a host-contract
decision or a design change, not a patch under time pressure.

# v0.3.1

Fail-open hardening of the compression wrapper. The wrapper now degrades to a
passthrough instead of turning its own bookkeeping into a host-visible failure.

## Fixed

- **Budget planning is now inside the fail-open boundary.** `build_budget_plan`
  ran before any try/except in `compress()`; a wrapper-side raise there denied
  the host a compression it would otherwise have gotten. Planning failure now
  degrades to a passthrough with no plan and is counted in
  `health["audit_failures"]`.
- **Post-delegate bookkeeping can no longer discard a successful compression.**
  Fingerprinting and the checkpoint-refresh hint ran *after* the delegate had
  already returned a valid result; a raise there propagated to the host and
  threw the completed compression away. Bookkeeping is now compute-then-commit:
  on failure the delegate's result is still returned, and no half-registered
  pending operation is left behind.
- **Orphan teardown is non-fatal.** A pending operation whose
  `_close_pending_on_teardown` raise previously wedged the next `compress()`.
  The unwedgeable orphan is now dropped and counted instead.
- **No zeroed journal rows.** When planning fails there is nothing truthful to
  journal, so the `started` row is skipped rather than written with zeroed
  totals.

## Notes

- Reuses `health["audit_failures"]`; the health schema is unchanged.
- The checkpoint-refresh cadence is unchanged: the counter still advances
  before the rule that reads it (regression-covered).
- Regression suite: `tests/test_fail_open_compress.py` (5 red-verified tests
  plus a happy-path no-regression guard).

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
