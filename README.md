# Hermes Context Tuner

**Upgrade-resilient context optimization for [Hermes Agent](https://hermes-agent.nousresearch.com/).**

Hermes Context Tuner helps long-running Hermes agents keep their working context useful, recoverable, and cheaper to carry forward.

It does **not** replace Hermes with a new memory system. It wraps Hermes' official `ContextEngine` interface, delegates actual summarization/compression to Hermes' live built-in `ContextCompressor`, and adds the missing operational layer around it:

- budget-aware context packing plans
- recovery/audit records for compression events
- a `/context-tuner` command
- a `context_tuner_status` context-engine tool
- an `optimize-context` skill example so agents learn when and how to use it automatically
- an upgrade-safe installer that can reapply the tiny Hermes shim after Hermes updates

The goal is simple:

> Keep standard Hermes compression, but make it observable, recoverable, and agent-operable.

---

## Why this exists

Long agent sessions eventually hit context pressure. Standard compression solves the immediate problem — fitting the next model request — but it usually leaves a few operational gaps:

1. **The agent cannot easily inspect what happened.**
   Compression may succeed, but later turns do not have an easy audit trail of message counts, rough token pressure, or what classes of messages were preserved/summarized.

2. **Recovery is implicit.**
   Hermes preserves session lineage in `state.db`, but agents need a simple pointer system that says, “this compressed segment came from that session boundary.”

3. **Context hygiene is not proceduralized.**
   Agents benefit from a skill that tells them when to compress, when to use focused compression, when to start fresh, and how to check whether compression actually helped.

4. **Custom patches are brittle.**
   Editing Hermes core directly can work today and break after the next update. Context Tuner avoids that by using Hermes' existing context-engine extension seam.

Hermes Context Tuner is the layer around compression: planning, audit, recovery pointers, status, and agent workflow.

---

## Standard Hermes compression vs. Hermes Context Tuner

| Capability | Standard Hermes compression | Hermes Context Tuner |
|---|---:|---:|
| Fits long sessions back into model context | ✓ | ✓, delegated to Hermes |
| Uses Hermes' native compressor/session rotation | ✓ | ✓ |
| Requires core Hermes patch | ✗ | ✗ |
| Installs through Hermes context-engine seam | Built-in | ✓ |
| Budget-aware advisory plan | ✗ | ✓ |
| Compression event audit DB | ✗ | ✓ |
| Recovery pointer sidecar | implicit only | ✓ |
| Agent-callable status tool | ✗ | ✓ |
| `/context-tuner` command | ✗ | ✓ |
| Skill example for automatic context hygiene | ✗ | ✓ |
| Reinstallable after Hermes updates | N/A | ✓ |

Context Tuner is deliberately conservative: it does not fork compression or rewrite session rotation. It composes the system Hermes already trusts.

---

## How it works

Hermes already supports pluggable context engines:

```text
context.engine = <engine name>
plugins/context_engine/<engine>/
```

Context Tuner installs a tiny shim at:

```text
plugins/context_engine/context-tuner/
```

The shim imports the actual Python package:

```python
from hermes_context_tuner.engine import ContextTunerEngine
```

Then `ContextTunerEngine`:

1. Instantiates Hermes' current built-in `ContextCompressor` at runtime.
2. Introspects the live compressor constructor and passes only supported arguments.
3. Implements Hermes' `ContextEngine` methods:
   - `update_from_response`
   - `should_compress`
   - `compress`
   - `on_session_start`
   - `on_session_end`
   - `get_status`
   - `get_tool_schemas`
   - `handle_tool_call`
4. Before compression, builds an advisory `BudgetPlan`:
   - protected head
   - protected recent tail
   - old middle messages to summarize
   - large tool outputs that should be externalized/pruned when possible
5. Delegates actual compression to the live Hermes compressor.
6. Records a best-effort sidecar audit event in:

```text
~/.hermes/context_tuner/recovery.sqlite
```

Audit writes are best-effort. If the sidecar DB fails, compression still succeeds.

---

## Install

### From source / local checkout

```bash
git clone https://github.com/tier4research/hermes-context-tuner.git
cd hermes-context-tuner
python -m pip install .
hermes-context-tuner install-hermes --set-config
```

Restart Hermes after installation:

```bash
hermes gateway restart
```

If you are inside a running gateway-controlled Hermes session, restart from an external shell. A gateway process cannot safely restart itself.

### Editable development install

```bash
cd hermes-context-tuner
python -m pip install -e .
hermes-context-tuner install-hermes --set-config
```

### Manual config

If the installer cannot write config, set this manually in `~/.hermes/config.yaml`:

```yaml
context:
  engine: context-tuner
```

Then restart Hermes.

---

## Verify installation

Run a simple Hermes command after restart:

```bash
hermes chat -q "Return exactly: context tuner smoke ok" --source tool --quiet
```

You should still get a normal answer. Context Tuner should be transparent during ordinary turns.

Inside a Hermes session, after compression has happened, run:

```text
/context-tuner
```

Expected shape:

```text
Hermes Context Tuner recent compression events:
- #1 messages 120 -> 24; ~182000 tokens; externalize_hint=8, keep_raw=12, summarize=100
```

If no compression has happened yet:

```text
Hermes Context Tuner is installed. No compression events recorded yet.
```

If the `context_engine` toolset is enabled, the agent can also call:

```text
context_tuner_status
```

---

## Integrating it so the agent uses it automatically

Context Tuner gives the agent two things:

1. A runtime status surface: `/context-tuner` and `context_tuner_status`.
2. A procedural skill: `optimize-context`.

Install or copy the example skill:

```text
examples/skills/optimize-context/SKILL.md
```

Into your Hermes skills directory, for example:

```text
~/.hermes/skills/optimize-context/SKILL.md
```

Then reload skills or start a fresh session.

The skill teaches the agent to:

- notice user phrases like “optimize context,” “lower usage,” “compact this,” or “we're getting bloated”
- audit before compressing
- prefer `/compress <focus>` when a particular project/thread matters
- check `/context-tuner` after compression
- preserve raw transcript as source of truth
- start a fresh session when repeated compression is degrading quality
- use session search/recovery pointers instead of carrying every old detail forward forever

### Recommended agent policy

For agents using Hermes Context Tuner, add a short operating rule like this to the agent's persona or project instructions:

```text
When context pressure is high or the user asks to optimize context, load the optimize-context skill. Check Context Tuner status before and after compression. Prefer focused /compress <topic> when preserving a specific work thread matters. Do not summarize away unrecoverable details; rely on Hermes session lineage and Context Tuner recovery pointers for old detail.
```

### Recommended workflow

```text
User: optimize context so we can keep going cheaply
Agent:
  1. Load optimize-context skill.
  2. Run /context-tuner or context_tuner_status if available.
  3. If pressure is high, run /compress <current project/focus>.
  4. Check /context-tuner again.
  5. Give the user a compact handoff: what survived, what can be recovered, what changed.
```

---

## Agent integration example

The package provides a Hermes context engine, not a tool you call manually from Python.

After installation/configuration, Hermes loads it as the active context engine:

```yaml
context:
  engine: context-tuner
```

At runtime, the agent sees the normal Hermes behavior. When compression triggers, Context Tuner records audit metadata and exposes status.

For custom agents built on Hermes internals, the rough equivalent is:

```python
from hermes_context_tuner.engine import ContextTunerEngine

engine = ContextTunerEngine()
engine.on_session_start(session_id="example")

if engine.should_compress(prompt_tokens):
    messages = engine.compress(messages, current_tokens=prompt_tokens)

print(engine.get_status())
```

Most users should not need that. Use the installer and Hermes config instead.

---

## Upgrade resilience model

| Risk | Mitigation |
|---|---|
| Hermes update changes compressor constructor | `ContextTunerEngine` introspects the live `ContextCompressor` signature and passes only supported kwargs. |
| Hermes update changes compression internals | Context Tuner delegates through the public `ContextEngine.compress()` contract instead of private internals. |
| Hermes update removes local context-engine shim | Rerun `hermes-context-tuner install-hermes --set-config`; the shim is generated from package source. |
| Audit DB failure | Compression still succeeds. Audit writes are best-effort and never block compression. |
| Hermes context-engine API breaks | Failure is explicit at import/startup; pin Hermes or update Context Tuner. |

This package is built to survive ordinary Hermes updates. It cannot guarantee compatibility with a breaking removal of Hermes' `ContextEngine` interface, but it avoids the common fragile paths: monkeypatching, private method wrapping, and core-file edits.

---

## Files and data

Context Tuner stores audit metadata here:

```text
~/.hermes/context_tuner/recovery.sqlite
```

It stores:

- old/new session IDs when available
- original/compressed message counts
- rough token estimate
- advisory budget decisions
- summary counts by decision type

It does **not** store raw message content. Raw transcript recovery remains Hermes' job through `state.db` and session lineage.

---

## Relationship to lossless-claw

[`@martian-engineering/lossless-claw`](https://github.com/Martian-Engineering/lossless-claw) is a full OpenClaw lossless context-management engine with DAG summaries and expansion tools.

Hermes Context Tuner is intentionally narrower:

| Capability | Hermes Context Tuner | lossless-claw |
|---|---:|---:|
| Target host | Hermes Agent | OpenClaw |
| Replaces host context engine | Wrapper around Hermes compressor | Yes, full context engine |
| DAG summarization | No | Yes |
| Recovery/audit pointers | Yes | Yes, via DAG/source links |
| Budget-aware advisory plan | Yes | Different design |
| Uses host-native compressor | Yes | No |
| Upgrade-resilient Hermes shim installer | Yes | N/A |

Credit: lossless-claw and LCM are prior art for lossless/DAG context management. This package does not copy that architecture.

---

## Development

```bash
python -m pip install -e .
PYTHONPATH=src python -m pytest tests -q -o 'addopts='
python -m build
```

Smoke-test the engine directly:

```bash
PYTHONPATH=src python - <<'PY'
from hermes_context_tuner.engine import ContextTunerEngine
engine = ContextTunerEngine()
print(engine.name)
print(engine.get_status())
PY
```

---

## Public alpha status

This is an alpha release. The package is intentionally small and conservative. The first goal is not to reinvent compression; it is to make Hermes compression easier for agents to operate, audit, and recover from.

Planned future improvements:

- richer recovery commands
- better externalization hints for large tool outputs
- optional docs for session-search recovery workflows
- CI matrix against multiple Hermes versions
- deeper status display in Hermes dashboard/plugin UI
