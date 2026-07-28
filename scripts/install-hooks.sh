#!/usr/bin/env bash
# Install pre-commit hook for this repo.
# Run:  bash scripts/install-hooks.sh
set -euo pipefail

HOOK_SRC="scripts/pre-commit.sh"
HOOK_DST=".git/hooks/pre-commit"

if [ ! -f "$HOOK_SRC" ]; then
    echo "❌ $HOOK_SRC not found. Run from the repository root."
    exit 1
fi

cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"
echo "✅ Pre-commit hook installed at $HOOK_DST"
echo "   (uses $HOOK_SRC as the source)"
