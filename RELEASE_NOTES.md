# v0.1.0-alpha.1

Initial public alpha of Hermes Context Tuner.

## Included

- `hermes_context_tuner.budget` advisory budget-plan library
- `RecoveryPointerStore` SQLite sidecar for compression audit/recovery metadata
- `ContextTunerEngine`, an official Hermes `ContextEngine` wrapper that delegates to the live built-in compressor
- `hermes-context-tuner install-hermes --set-config` installer for the Hermes context-engine shim
- `/context-tuner` command through the context-engine command bridge
- `context_tuner_status` context-engine tool
- `optimize-context` Hermes skill example

## Upgrade posture

The package does not patch Hermes core or monkey-patch private methods. It uses the official context-engine seam and can be reinstalled after Hermes updates by rerunning the installer.
