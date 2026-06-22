# Contributing

Thanks for helping improve Hermes Context Tuner.

## Development setup

```bash
git clone https://github.com/tier4research/hermes-context-tuner.git
cd hermes-context-tuner
python -m pip install -e .
PYTHONPATH=src python -m pytest tests -q -o 'addopts='
```

## Design principles

1. **Do not fork Hermes compression.** Delegate to Hermes' built-in compressor unless there is a very explicit reason not to.
2. **Do not monkey-patch Hermes private methods.** Use the `ContextEngine` seam.
3. **Audit must never block compression.** Recovery/audit sidecar writes are best-effort.
4. **Store metadata, not raw transcript content.** Raw session data belongs to Hermes `state.db`.
5. **Prefer upgrade resilience over cleverness.** A small shim that can be reinstalled after updates is better than a broad core patch.

## Tests

```bash
PYTHONPATH=src python -m pytest tests -q -o 'addopts='
python -m build
```

## Commit style

Use short conventional-ish messages:

```text
feat: add recovery pointer listing
fix: keep audit failures non-blocking
docs: clarify Hermes integration
```
