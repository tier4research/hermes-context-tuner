---
name: optimize-context
description: Operate Hermes Context Tuner — audit context pressure, compress deliberately, and recover compressed-session pointers.
version: 0.1.0
author: Tier 4 Research
license: Apache-2.0
metadata:
  hermes:
    tags: [hermes, context, compression, optimization]
---

# Optimize Context

Use this skill when:

- the user says "optimize context", "lower usage", "compact this", or "we're getting bloated"
- a session is near the compression threshold
- large tool outputs have accumulated
- a long engineering session needs a clean handoff without losing recovery paths

## Procedure

1. **Audit first**
   - If available, run `/context-tuner` or call `context_tuner_status`.
   - Check compression count, recovery-store path, and recent compression events.

2. **Prefer existing Hermes compression**
   - Use `/compress` for manual compaction.
   - Use `/compress <focus>` when a specific project/thread matters.
   - Do not rewrite raw transcript files unless the user explicitly asks.

3. **Keep the raw transcript as source of truth**
   - Hermes Context Tuner records recovery/audit pointers.
   - The original detail remains in Hermes `state.db` session lineage.

4. **After compression**
   - Check `/context-tuner` again.
   - Confirm a new event exists and message count reduced or explain why no-op occurred.

5. **When compression is not enough**
   - Start a new session with a compact handoff.
   - Use session search or recovery pointers to pull back old details only when needed.

## Safety notes

- Context Tuner should not break prompt caching more than Hermes compression already does.
- Audit/recovery writes are best-effort and should never block compression.
- If a Hermes update removes the context-engine shim, rerun:

```bash
hermes-context-tuner install-hermes --set-config
```
