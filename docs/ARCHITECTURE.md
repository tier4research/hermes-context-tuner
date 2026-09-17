# Architecture

Hermes Context Tuner is a small wrapper around Hermes Agent's existing context engine interface.

## Components

```text
Hermes Agent
  └─ context.engine = context-tuner
      └─ plugins/context_engine/context-tuner/__init__.py  # tiny shim
          └─ hermes_context_tuner.engine.ContextTunerEngine
              ├─ delegates to agent.context_compressor.ContextCompressor
              ├─ builds advisory BudgetPlan
              └─ writes RecoveryPointerStore metadata
```

## Why a wrapper?

Hermes already owns the dangerous parts:

- when compression triggers
- how summaries are produced
- how session rotation works
- how parent session lineage is preserved
- how memory providers are notified

Context Tuner should not duplicate or fork that logic. It adds observability and operational guidance around the compressor.

## Compatibility seam

The public seam is Hermes' `ContextEngine` protocol:

- `update_from_response()`
- `should_compress()`
- `compress()`
- `on_session_start()`
- `on_session_end()`
- `get_status()`
- optional tools/commands

The wrapper instantiates Hermes' live `ContextCompressor` at runtime and filters constructor kwargs by inspecting the live signature. This avoids pinning to one Hermes constructor version.

## Data model

The sidecar SQLite DB (default `~/.hermes/context_tuner/recovery.sqlite`, schema
version 3 via `PRAGMA user_version`) has two lineage tables:

- `compression_events` — one row per compression operation with a full lifecycle:
  `operation_id` (unique), `status` (`started → finalized | failed | aborted |
  interrupted`), pre/post counts and token estimates, trigger metadata,
  budget fields, source/result fingerprints, a summary-marker pointer, and a
  `checkpoint_required` flag when semantic content was summarized or
  externalized without a recent explicit checkpoint.
- `recovery_pointers` — per-message/range decisions with roles, token
  estimates, decisions, reasons, and content fingerprints.

A second sidecar (`checkpoints.sqlite`) holds the explicit, append-only
checkpoint ledger (`checkpoints`, `decision_events`, `task_events`). A
correction creates a new event with a supersession link; nothing is edited
in place. Checkpoints, decision events, and task transitions written together
through `context_tuner_checkpoint` share a single transaction, so a rejected
entry persists nothing.

Both stores keep metadata and hashes only. They never store raw transcript
content, credentials, or model reasoning. The per-message fingerprints are
chained into the event-level `source_fingerprint`, so it changes whenever any
source message changes (a count/token digest would not), and a failure reason
records only the exception class name.

## Bounded resume packet

`context_tuner_resume` reconstructs project state from the ledger and returns
it hard-capped at 8 KB. The cap is enforced by ordered shedding — optional
sections (`tasks`, `last_compression`, `decisions`) first, then the
checkpoint's list fields, then the checkpoint reduced to scalar identity —
with envelope keys bounded by `MAX_RESUME_KEY_CHARS`. The packet never carries
transcript content.

## Compression lineage protocol

Recording is two-phase, matching Hermes' host contract:

1. `compress()` opens a `started` operation (with pointers/fingerprints of
   the pre-compression messages) before delegating to Hermes' compressor.
2. Hermes calls `on_session_start(new_id, boundary_reason="compression",
   old_session_id=parent, conversation_id=...)` **after committing** the
   compression child session. Only then is the operation marked `finalized`
   with the new session ID.
3. A delegate exception marks the operation `failed` and propagates
   unchanged. A session end/reset, or a new non-compression session start,
   marks a still-open operation `interrupted` — a compressor return value
   alone is never treated as a commit.
4. The boundary is correlated to the pending operation: if the callback's
   `old_session_id` does not match the operation's parent session, the
   operation is marked `interrupted` rather than finalized with the wrong
   lineage.
5. If the finalize write itself fails, the operation is closed as
   `interrupted` via `mark_interrupted_if_started` (state-guarded, so it cannot
   corrupt a terminal row). Operations orphaned by process death are swept at
   engine startup by an age-gated reconciliation, which never touches a live
   in-flight operation.

## Failure policy

Compression must win over audit.

If budget planning or audit persistence fails, the wrapper continues to
delegate to Hermes compression (fail-open), counts the failure in
`get_status()['health']`, and degrades status views visibly. A context
optimization plugin that breaks the conversation is worse than no plugin.

## Migration & rollback

Migrations are additive and idempotent, keyed on `PRAGMA user_version`.
Version 3 backfills lifecycle columns from the legacy/v2 shapes without
destroying rows. Unsupported future versions raise `SchemaUnsupported`
instead of rewriting. To roll back: stop Hermes, restore the pre-migration
backup (taken with SQLite's backup API) over `recovery.sqlite`. There is no
down-migration.
