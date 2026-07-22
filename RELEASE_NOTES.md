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
