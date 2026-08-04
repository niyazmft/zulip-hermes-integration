#!/usr/bin/env bash
# update-plugin.sh — Update the Zulip Hermes plugin from GitHub.
#
# Usage:
#   ./update-plugin.sh              # check + download + verify + install
#   ./update-plugin.sh --check-only # just check, don't install
#
# This runs the updater as a standalone CLI script (no chat command needed).
# Files are verified via SHA-256 checksums before replacement.
# A gateway restart is required after update.

set -euo pipefail

cd "$(dirname "$0")"

echo "═══════════════════════════════════════"
echo "  Zulip Hermes Plugin Updater"
echo "═══════════════════════════════════════"

python3 -m zulip.updater "$@"

echo ""
echo "═══════════════════════════════════════"
echo "  Done. Restart the Hermes gateway:"
echo "    hermes gateway restart"
echo "═══════════════════════════════════════"
