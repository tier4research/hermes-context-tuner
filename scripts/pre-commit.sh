#!/usr/bin/env bash
# Pre-commit hook — blocks commits with sensitive content leaks.
# Install:  cp scripts/pre-commit.sh .git/hooks/pre-commit
# Auto-install:  bash scripts/install-hooks.sh
#
# Uses scripts/leak_scan.py from hermes-agent (preferred) or falls back
# to grep-based detection.

set -euo pipefail

SCAN_SCRIPT="${LEAK_SCAN_PATH:-}"

# Try to find leak_scan.py
if [ -z "$SCAN_SCRIPT" ]; then
    CANDIDATES=(
        "$HOME/AppData/Local/hermes/scripts/leak_scan.py"
        "$HOME/.hermes/scripts/leak_scan.py"
        "/opt/hermes/scripts/leak_scan.py"
        "./scripts/leak_scan.py"
    )
    for c in "${CANDIDATES[@]}"; do
        if [ -f "$c" ]; then
            SCAN_SCRIPT="$c"
            break
        fi
    done
fi

if [ -n "$SCAN_SCRIPT" ] && [ -f "$SCAN_SCRIPT" ]; then
    # Use the full scanner — handle MSYS→Windows path translation
    REPO_ROOT="$(git rev-parse --show-toplevel)"
    PYTHON_CMD="python3"
    SCRIPT_PATH="$SCAN_SCRIPT"
    # On Windows/MSYS, convert paths to Win32 format for Python
    if command -v cygpath &>/dev/null; then
        REPO_ROOT="$(cygpath -w "$REPO_ROOT")"
        SCRIPT_PATH="$(cygpath -w "$SCAN_SCRIPT")"
    fi
    # Prefer uv for reliable Python invocation
    if command -v uv &>/dev/null; then
        if uv run python -c "import sys; sys.exit(0)" &>/dev/null 2>&1; then
            exec uv run python "$SCRIPT_PATH" --staged --repo "$REPO_ROOT"
        fi
    fi
    exec $PYTHON_CMD "$SCRIPT_PATH" --staged --repo "$REPO_ROOT"
else
    # Fallback: grep staged changes for obvious leaks
    echo "⚠️  leak_scan.py not found — using grep-based fallback."
    echo "   Install: cp scripts/pre-commit.sh .git/hooks/pre-commit"
    echo "   (Set LEAK_SCAN_PATH env var to point to leak_scan.py)"
    echo ""

    FAIL=0
    STAGED=$(git diff --cached --name-only --diff-filter=ACMR)
    for f in $STAGED; do
        # Check filename
        case "$(basename "$f")" in
            .env|*.pem)
                echo "🔴 LEAK: Suspicious filename: $f"
                FAIL=1
                ;;
        esac
        # Check content (text files only)
        case "$f" in
            *.py|*.md|*.txt|*.json|*.yaml|*.yml|*.toml|*.cfg|*.ini)
                if git show ":$f" | grep -qE 'C:\\Users\\Admin|/c/Users/Admin|93\.188\.161\.20|~\.mempalace-sqlite-vec'; then
                    echo "🔴 LEAK: Internal path or IP in $f"
                    git show ":$f" | grep -nE 'C:\\Users\\Admin|/c/Users/Admin|93\.188\.161\.20|~\.mempalace-sqlite-vec' | head -5
                    FAIL=1
                fi
                if git show ":$f" | grep -qE '\bElle\b'; then
                    echo "🔴 LEAK: Internal name reference in $f (possible private identifier)"
                    git show ":$f" | grep -nE '\bElle\b' | head -5
                    FAIL=1
                fi
                ;;
        esac
    done

    if [ $FAIL -eq 1 ]; then
        echo ""
        echo "❌ Commit blocked: sensitive content detected."
        echo "   Address the issues above or bypass with: git commit --no-verify"
        exit 1
    fi
    echo "✅ No leaks detected (fallback scan)."
fi
