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

The sidecar SQLite DB has two tables:

- `compression_events`
- `recovery_pointers`

It stores metadata only. It does not store raw transcript content.

## Failure policy

Compression must win over audit.

If budget planning or audit persistence fails, the wrapper should continue to delegate to Hermes compression. A context optimization plugin that breaks the conversation is worse than no plugin.
