# Security Policy

Hermes Context Tuner is a local Hermes Agent context-engine wrapper. It does not make network calls and does not execute tools by itself.

## Data handling

Context Tuner writes audit/recovery metadata to:

```text
~/.hermes/context_tuner/recovery.sqlite
```

The sidecar database records metadata such as:

- session IDs
- message counts
- rough token estimates
- budget-plan decisions

It should not store raw message contents, API keys, tokens, passwords, or tool outputs.

## Reporting issues

For public issues, avoid posting:

- API keys
- auth tokens
- private `config.yaml` or `.env` content
- raw transcripts containing sensitive data

Open a GitHub issue with a sanitized reproduction, Hermes version, Context Tuner version, and the relevant traceback.

## Threat model

This package is designed to be low-risk:

- no network calls
- no shell execution except the explicit installer command
- no raw transcript storage
- audit writes are best-effort and non-blocking

The main security risk is accidental leakage through bug reports or logs. Sanitize before sharing.
