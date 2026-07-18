#!/usr/bin/env bash
# Install git hooks from .githooks/ into .git/hooks/

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.githooks"
GIT_HOOKS_DIR="$REPO_ROOT/.git/hooks"

echo "Installing Zulip Hermes git hooks..."

for hook in "$HOOKS_DIR"/*; do
    if [ -f "$hook" ]; then
        hook_name="$(basename "$hook")"
        target="$GIT_HOOKS_DIR/$hook_name"
        cp "$hook" "$target"
        chmod +x "$target"
        echo "  ✓ installed $hook_name"
    fi
done

echo "Done. Hooks will run on every git push."
echo "To skip hooks temporarily: git push --no-verify"
