# Hermes Context Tuner

Upgrade-resilient context optimization for [Hermes Agent](https://hermes-agent.nousresearch.com/).

Hermes already has a good built-in compressor. Hermes Context Tuner does **not** fork it. It wraps Hermes' official `ContextEngine` interface, delegates actual compression to the live built-in `ContextCompressor`, and adds:

- budget-aware context pack plans
- compression/recovery audit records
- a `context_tuner_status` context-engine tool
- a `/context-tuner` command
- a Hermes skill example for operating context pressure deliberately

That means it keeps working across normal Hermes updates as long as Hermes keeps the stable `ContextEngine` contract. If Hermes updates delete local in-repo context-engine shims, just rerun the installer; no core patch is required.

## Why not patch Hermes core?

Because Hermes already has the right extension seam:

```text
context.engine = <engine name>
plugins/context_engine/<engine>/
```

The package installs a tiny shim into that context-engine directory. The shim imports the pip/local package implementation. The implementation then composes Hermes' current compressor at runtime.

No monkey patching. No private method wrapping. No replacement of session rotation logic.

## Install locally

```bash
cd "C:/Users/Admin/Documents/New project/hermes-context-tuner"
python -m pip install -e .
hermes-context-tuner install-hermes --set-config
```

Then restart Hermes or the gateway.

## Verify

```bash
hermes config set context.engine context-tuner
hermes chat -q "Say hello" --ignore-rules --source tool
```

Inside a session, after compression has happened:

```text
/context-tuner
```

Or use the context-engine tool if enabled:

```text
context_tuner_status
```

## Upgrade resilience model

| Risk | Mitigation |
|---|---|
| Hermes update changes compressor constructor | `ContextTunerEngine` introspects the live `ContextCompressor` signature and passes only supported kwargs. |
| Hermes update deletes copied shim | rerun `hermes-context-tuner install-hermes --set-config`; shim is generated from package source. |
| Hermes changes compression internals | tuner delegates through the public `ContextEngine.compress()` contract instead of private compression internals. |
| Audit DB failure | compression still succeeds; audit writes are best-effort and never block compression. |
| Context-engine API breaks | package degrades at import/test time; version pinning/reinstall is explicit. |

## Relationship to lossless-claw

[`@martian-engineering/lossless-claw`](https://github.com/Martian-Engineering/lossless-claw) is a full OpenClaw lossless context-management engine with DAG summaries and expansion tools. Hermes Context Tuner is intentionally narrower:

| Capability | Hermes Context Tuner | lossless-claw |
|---|---:|---:|
| Target host | Hermes Agent | OpenClaw |
| Replaces host context engine | Wrapper around built-in compressor | Yes, full context engine |
| DAG summarization | No | Yes |
| Recovery/audit pointers | Yes | Yes, via DAG/source links |
| Budget-aware advisory plan | Yes | Partial/different design |
| Uses host-native compressor | Yes | No |
| Upgrade-resilient shim installer | Yes | N/A |

Credit: lossless-claw and the LCM work are prior art for lossless/DAG context management. This package does not copy that architecture.

## Public alpha status

This is an alpha package. The core library and shim are small on purpose. Future work can add richer externalization and recovery commands if Hermes exposes more context-lifecycle metadata.
