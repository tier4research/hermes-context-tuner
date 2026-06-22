# Integration Guide

This guide explains how to install Hermes Context Tuner so a Hermes agent can use it automatically.

## 1. Install the package

```bash
python -m pip install hermes-context-tuner
```

For a local checkout:

```bash
cd hermes-context-tuner
python -m pip install -e .
```

## 2. Install the Hermes context-engine shim

```bash
hermes-context-tuner install-hermes --set-config
```

This copies a tiny shim into:

```text
<hermes repo>/plugins/context_engine/context-tuner/
```

and sets:

```yaml
context:
  engine: context-tuner
```

If config writing fails, edit `~/.hermes/config.yaml` manually.

## 3. Restart Hermes

CLI users: exit and start a new `hermes` session.

Gateway users:

```bash
hermes gateway restart
```

Run this from an external shell, not from inside the gateway session.

## 4. Install the skill example

Copy:

```text
examples/skills/optimize-context/SKILL.md
```

to:

```text
~/.hermes/skills/optimize-context/SKILL.md
```

Then run `/reload-skills` or start a fresh session.

## 5. Agent operating rule

Add this to the agent's durable instructions or project `AGENTS.md`:

```text
When context pressure is high or the user asks to optimize context, load the optimize-context skill. Check /context-tuner or context_tuner_status before and after compression. Prefer focused /compress <topic> when preserving a specific work thread matters. Use recovery pointers/session search for old detail instead of carrying every old tool output forward.
```

## 6. Verify

```bash
hermes chat -q "Return exactly: context tuner smoke ok" --source tool --quiet
```

Inside a session:

```text
/context-tuner
```

No compression yet:

```text
Hermes Context Tuner is installed. No compression events recorded yet.
```

After compression:

```text
Hermes Context Tuner recent compression events:
- #1 messages 120 -> 24; ~182000 tokens; externalize_hint=8, keep_raw=12, summarize=100
```

## Upgrade notes

After `hermes update`, if the context-engine shim disappears, rerun:

```bash
hermes-context-tuner install-hermes --set-config
```

The package code remains installed; the shim is intentionally disposable.
