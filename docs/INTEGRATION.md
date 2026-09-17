# Integration Guide

This guide explains how to install Hermes Context Tuner so a Hermes agent can use it automatically.

## 1. Install the package

```bash
python -m pip install hermes-context-tuner
```

For a local checkout:

```bash
cd hermes-context-tuner
python -m pip install -e .
```

## 2. Install the Hermes context-engine shim

```bash
hermes-context-tuner install-hermes --set-config
```

This copies a tiny shim into:

```text
<hermes repo>/plugins/context_engine/context-tuner/
```

and sets:

```yaml
context:
  engine: context-tuner
```

If config writing fails, edit `~/.hermes/config.yaml` manually.

## 3. Restart Hermes

CLI users: exit and start a new `hermes` session.

Gateway users:

```bash
hermes gateway restart
```

Run this from an external shell, not from inside the gateway session.

## 4. Install the skill example

Copy:

```text
examples/skills/optimize-context/SKILL.md
```

to:

```text
~/.hermes/skills/optimize-context/SKILL.md
```

Then run `/reload-skills` or start a fresh session.

## 5. Agent operating rule

Add this to the agent's durable instructions or project `AGENTS.md`:

```text
When context pressure is high or the user asks to optimize context, load the optimize-context skill. Check /context-tuner or context_tuner_status before and after compression. Prefer focused /compress <topic> when preserving a specific work thread matters. Use recovery pointers/session search for old detail instead of carrying every old tool output forward.
```

## 6. Verify

```bash
hermes chat -q "Return exactly: context tuner smoke ok" --source tool --quiet
```

Inside a session:

```text
/context-tuner
```

No compression yet:

```text
Hermes Context Tuner is installed. No compression events recorded yet.
```

After compression:

```text
Hermes Context Tuner recent compression events:
- #1 messages 120 -> 24; ~182000 tokens; externalize_hint=8, keep_raw=12, summarize=100
```

## Upgrade notes

After `hermes update`, if the context-engine shim disappears, rerun:

```bash
hermes-context-tuner install-hermes --set-config
```

The package code remains installed; the shim is intentionally disposable.

## Checkpoints and resume (context continuity)

The package adds two agent tools and matching CLI subcommands so work can
survive context compression and deliberate session refreshes.

### Write a checkpoint (agent tool)

```text
context_tuner_checkpoint {
  "project_key": "my-project",          // required, stable key
  "goal": "ship the migration",          // required
  "current_stage": "code review",        // required
  "status": "in_progress",               // required: in_progress|blocked|done|abandoned
  "task_id": "migration-1",              // optional
  "conversation_id": "<gateway key>",    // optional scoping
  "completed": ["..."], "locked_decisions": ["..."], "hard_constraints": ["..."],
  "unresolved": ["..."], "next_actions": ["..."],
  "artifacts": [{"path": "src/x.py", "sha256": "<64 hex>"}],
  "provenance": "why/where this was written",
  "decisions": [{"content": "use user_version", "supersedes": "<decision_id>"}],
  "task_updates": [{"task_id": "migration-1", "status": "in_progress", "note": "started"}]
}
```

Returns `{"ok": true, "checkpoint_id": "...", "decision_ids": [...], "tasks_recorded": N}`.
Payloads are validated and hard-capped (≤100 list items, ≤1000 chars per string,
≤16 KB total); rejections are atomic and reported, not silently truncated. The
checkpoint, its decision events, and its task transitions are written in a
single transaction — if any entry is rejected, nothing is written.

### Resume after compression or a new session (agent tool)

```text
context_tuner_resume {"project_key": "my-project", "conversation_id": "...", "new_session_id": "..."}
```

Returns a packet hard-capped at 8 KB: the latest checkpoint, active decisions
(with supersessions applied), task status, the last compression pointer — and
no transcript content. When the packet would exceed the cap it is shed in
order (optional sections, then checkpoint list fields, then checkpoint
scalars) and marked `"truncated": true`; the checkpoint id is preserved even
then, and `project_key` is limited to 200 characters. The session-continuity
skill should call this at startup when a checkpoint exists.

### CLI equivalents

```bash
hermes-context-tuner checkpoint --file cp.json
hermes-context-tuner resume --project my-project [--conversation KEY]
```

### Storage

```text
~/.hermes/context_tuner/recovery.sqlite      # compression lineage (schema v3)
~/.hermes/context_tuner/checkpoints.sqlite   # explicit checkpoint ledger
```

### Rollout mode

Journaling and checkpointing are active whenever the package is installed;
compression behavior is unchanged either way (the journal is fail-open and
off the critical path). There is no automatic `/new`, no automatic session
reset, and Hermes' built-in compression semantics are untouched. To disable
entirely, remove the engine shim or uninstall the package — the sidecar
files are inert history and can be restored from backup.

### Optional rolling-context policy

`BudgetPolicy` is opt-in. Construct the engine with one to make its
`checkpoint_needed()` signal authoritative for the journal's
`checkpoint_required` flag:

```python
from hermes_context_tuner.budget import BudgetPolicy
ContextTunerEngine(budget_policy=BudgetPolicy(...))
```

Without a policy the default rule applies: flag a required checkpoint after
≥ 3 compressions since the last explicit checkpoint. Either way the flag is
advisory — nothing schedules or forces a session reset.

### Operations notes

- Failure reasons stored in the lineage DB are content-free (the exception
  class name only); exception messages are never persisted.
- An operation orphaned by a process crash is reconciled to `interrupted` at
  the next engine startup once it is older than the age gate. Tune the gate
  with `HERMES_CONTEXT_TUNER_STALE_OP_SECONDS` (seconds, default `3600`).
- The resume `project_key` must be ≤ 200 characters.

### Watchdog compatibility

`compression_watchdog.py` (which reads only `status='finalized'` events)
continues to work unchanged; failed/interrupted/aborted operations are
excluded from its alerting by design.
