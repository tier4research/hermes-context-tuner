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
