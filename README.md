# Hermes Context Tuner

[![Tests](https://github.com/tier4research/hermes-context-tuner/actions/workflows/tests.yml/badge.svg)](https://github.com/tier4research/hermes-context-tuner/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Status: Beta](https://img.shields.io/badge/status-beta-green.svg)](RELEASE_NOTES.md)

**Know what your agent's compressor threw away.**

[Hermes Agent](https://hermes-agent.nousresearch.com) is an open-source agent framework. Like every long-running agent, it compresses session history when context gets tight — and like every compressor, it sometimes summarizes away the wrong thing. A key instruction, a project spec, a user preference. Then the agent starts answering confidently wrong, and you have no record of what was dropped.

Context Tuner doesn't replace that compressor. It wraps it and writes down what it did: which messages were kept raw, which were summarized, which were flagged as oversized tool output, and which session each compressed segment came from. Then it exposes that history to you *and to the agent itself*, so the agent can check its own context hygiene mid-run.

```bash
pip install hermes-context-tuner
hermes-context-tuner install-hermes --set-config
hermes gateway restart
```

Then, inside any session:

```
/context-tuner

Hermes Context Tuner recent compression events:
- #1 messages 120 -> 24; ~182000 tokens; externalize_hint=8, keep_raw=12, summarize=100
```

---

## What "recovery" means here — and what it doesn't

Context Tuner's sidecar stores metadata and pointers, not message text. For each compression event it records the counts, the token estimates, the per-message decision, and the source session ID.

So it can tell you that messages 40–90 were summarized in session `abc123`, and it can point you at that session. It does **not** restore the original text into your context automatically, and it is **not** a backup of your conversation. If you need the raw messages, you go read the session log — Context Tuner tells you which one and which range.

That's a narrower promise than "recoverable memory," and it's the one the tool actually keeps. The audit trail is the product; the pointers are a convenience on top of it.

---

## What it adds

**A budget plan you can inspect before compression happens.** Protect the first N messages, protect the last M tokens, mark large tool outputs for externalization, flag the middle for summarization. The plan is advisory — it shows what would happen before it does.

**An audit trail.** Every compression event lands in a SQLite sidecar with per-message decisions and reasons. Writes are best-effort: if the audit DB fails, compression still succeeds. Observability never blocks the agent.

**Something the agent can operate itself.** The `context_tuner_status` tool and the `/context-tuner` command let the agent inspect its own compression history. The bundled `optimize-context` skill gives it a procedure: audit → compress targeted → verify → hand off. No external dashboard.

**No monkey-patching.** Context Tuner attaches through Hermes' official `ContextEngine` interface and introspects the live compressor's constructor at runtime, passing only the kwargs it supports. A Hermes upgrade that changes that signature doesn't break you; one that removes the shim needs a single reinstall command.

---

## Upgrade resilience

| Risk | Mitigation |
|------|------------|
| Hermes update changes compressor constructor | Introspects live signature, passes only supported kwargs |
| Hermes update changes compression internals | Delegates through public `compress()` contract |
| Hermes update removes shim | Rerun `hermes-context-tuner install-hermes --set-config` |
| Audit DB failure | Compression succeeds; audit is best-effort |
| ContextEngine API breaks | Failure at import/startup — explicit, not silent |

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

## Status

**Beta** (v0.2.0-beta.1). Budget planning, the audit trail, status commands, and the Hermes integration are working and tested. Recovery is currently pointer-level only. Tested against Hermes v0.4.x — a CI matrix across versions is planned, so compatibility with other releases is unverified. 7 commits, 0 external stars.

---

## FAQ

### What is Hermes Context Tuner?
Hermes Context Tuner is a Python library that wraps an agent's context compressor and records what it did: which messages were kept raw, which were summarized, and which session each compressed segment came from. It provides an audit trail, a token-budget planner, and a status tool the agent can call itself.

### Does Context Tuner replace the built-in compressor?
No. It delegates compression to the agent's native `ContextCompressor`. What it adds is observability: a budget plan you inspect beforehand, an audit trail afterward, and recovery pointers to the original session logs.

### Can Context Tuner recover compressed messages?
It provides pointers — session ID and message range — not the raw text. If you need the original messages, you read the session log that Context Tuner points to.

### Does it break when Hermes updates?
This is explicitly designed not to. Context Tuner introspects the live compressor's constructor at runtime and passes only the kwargs it supports. If a Hermes update changes the interface, the tool fails at import/startup rather than silently corrupting the session.

### Does it work with agents other than Hermes?
Not currently. The integration is through Hermes' `ContextEngine` interface. A generic `ContextTunerEngine` core is feasible — the audit trail, budget planner, and recovery store are agent-agnostic — but the adapter layer is Hermes-specific today.

### What's the relationship with Memorant?
[Memorant](https://github.com/tier4research/memorant) is a separate project for *long-term* agent memory: claims with provenance, trust tiers, and atomic corrections in a single SQLite file. Context Tuner handles *short-term* working context compression. [Expectation Ledger](https://github.com/tier4research/expectation-ledger) is a circuit breaker for agent control loops — predict, compare, and self-block on divergence. All three are designed to complement each other — see the [Memorant suite workflow](https://github.com/tier4research/memorant#suite-workflow).

---

## License

Apache License 2.0.
