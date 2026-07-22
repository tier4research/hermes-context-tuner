# Hermes Context Tuner

[![Tests](https://github.com/tier4research/hermes-context-tuner/actions/workflows/tests.yml/badge.svg)](https://github.com/tier4research/hermes-context-tuner/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Status: Beta](https://img.shields.io/badge/status-beta-green.svg)](RELEASE_NOTES.md)

**Make your agent's long-term memory recoverable, auditable, and budget-aware — without forking or patching the agent itself.**

Every long-running AI agent session hits context pressure. The built-in compressor handles the immediate problem (fit the next model request), but it leaves a trail of silent questions: *What got compressed? Was it important? Can I get it back? Did my agent lose something it needed?*

Hermes Context Tuner wraps the agent's native compression engine and adds what's missing: an audit trail, a token-budget planner, a recovery pointer system, and a skill that teaches agents when and how to compress smartly.

It does **not** replace your agent's compressor. It makes it **observable, controllable, and recoverable.**

---

## The problem

Standard context compression works — until it doesn't. You get that one session where a critical instruction, a project spec, or a user preference got summarized away and now the agent is answering confidently wrong, and you have no way to know what was lost or how to get it back.

| Problem | What Context Tuner adds |
|---------|------------------------|
| **Invisible compression** — what got kept, summarized, or dropped? | Audited compression events with per-message decisions (kept raw / summarized / externalized) |
| **No recovery path** — can I find what was compressed? | Recovery pointer sidecar links compressed segments to their source sessions |
| **Blind token pressure** — how close to the limit are we? | `context_tuner_status` tool shows current budget, decisions, and history |
| **Agent doesn't know how to context-hygiene** | `optimize-context` skill — a procedural playbook for knowing when to compress, what to preserve, and when to start fresh |
| **Custom patches break on updates** | Upgrade-resilient shim that introspects the live compressor and adapts — no monkey-patching |

---

## What it does differently

**Budget-aware packing plans.** Before compression, Context Tuner builds an advisory plan: protect the first N messages (context head), protect the last M tokens (recent tail), mark large tool outputs for externalization, and flag the middle for summarization. The plan reveals *what would happen* before it happens.

**Compression audit trail.** Every compression event is recorded in a SQLite sidecar: original vs. compressed message count, token estimates, per-message decisions with reasons. Not raw messages — just metadata. Enough to know what was lost and where to find the original.

**Agent-operable.** The `/context-tuner` command and `context_tuner_status` tool let the agent inspect compression history itself. No external dashboard required. The built-in skill (`optimize-context`) teaches agents a complete context-hygiene workflow: audit → compress targeted → verify → hand off.

**Survives Hermes upgrades.** Context Tuner uses Hermes' official `ContextEngine` interface — it doesn't monkey-patch or edit core files. It introspects the live compressor's constructor signature at runtime and adapts. Reinstall after an upgrade with one command.

---

## Quick start

```bash
pip install hermes-context-tuner
hermes-context-tuner install-hermes --set-config
hermes gateway restart
```

Inside a session, after compression has happened:

```
/context-tuner
```

Expected output:

```
Hermes Context Tuner recent compression events:
- #1 messages 120 -> 24; ~182000 tokens; externalize_hint=8, keep_raw=12, summarize=100
```

---

## How it works

```
┌─────────────────────────────────────────────────────┐
│                  Context Tuner                       │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │
│  │ Budget Plan  │  │ Compression  │  │ Recovery  │  │
│  │ (advisory)   │──▶│  (delegated  │──▶│  Store    │  │
│  │              │  │   to Hermes) │  │  (audit)  │  │
│  └──────────────┘  └──────────────┘  └───────────┘  │
│                              │                       │
│                              ▼                       │
│                     ┌──────────────┐                 │
│                     │ Status Tool  │                 │
│                     │ /command     │                 │
│                     └──────────────┘                 │
└─────────────────────────────────────────────────────┘
```

1. Build an advisory `BudgetPlan` — protected head + tail, summarize middle, externalize large tool outputs
2. Delegate actual compression to Hermes' built-in `ContextCompressor`
3. Record decisions in the recovery sidecar (best-effort, never blocks compression)
4. Expose status via `/context-tuner` command and `context_tuner_status` tool

---

## Comparison

| Capability | Standard Hermes | + Context Tuner |
|---|---|---|
| Fits long sessions into model context | ✓ | ✓ (delegated to Hermes) |
| Requires core patch | — | — (uses official ContextEngine seam) |
| Budget-aware advisory plan | — | ✓ |
| Compression event audit DB | — | ✓ |
| Recovery pointer sidecar | implicit only | ✓ |
| Agent-callable status commands | — | ✓ |
| Context-hygiene skill | — | ✓ |
| Reinstallable after Hermes updates | N/A | ✓ |
| Survives compressor constructor changes | N/A | ✓ (introspects at runtime) |

---

## Developer use

```python
from hermes_context_tuner.engine import ContextTunerEngine

engine = ContextTunerEngine()
engine.on_session_start(session_id="example")

if engine.should_compress(prompt_tokens):
    messages = engine.compress(messages, current_tokens=prompt_tokens)

print(engine.get_status())
```

---

## Upgrade resilience

| Risk | Mitigation |
|---|---|
| Hermes update changes compressor constructor | Introspects live signature, passes only supported kwargs |
| Hermes update changes compression internals | Delegates through public `compress()` contract |
| Hermes update removes shim | Rerun `hermes-context-tuner install-hermes --set-config` |
| Audit DB failure | Compression succeeds; audit is best-effort |
| ContextEngine API breaks | Failure at import/startup — explicit, not silent |

---

## Status

**Beta** (v0.2.0-beta.1). Core features stable: budget planning, audit trail, status commands, Hermes integration. Planned improvements: richer recovery commands, better externalization hints for large tool outputs, CI matrix against multiple Hermes versions.

---

## License

Apache License 2.0.
