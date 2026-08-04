#!/usr/bin/env bash
# update.sh — One-command update for the Zulip Hermes plugin.
#
# Usage:
#   bash ~/.hermes/plugins/zulip/update.sh
#
# Downloads the latest plugin files from GitHub, verifies SHA-256 checksums,
# replaces files in-place, and restarts the Hermes gateway.
#
# This is the deployment script referenced in the README.

set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PLUGIN_DIR"

echo "═══════════════════════════════════════"
echo "  Zulip Hermes Plugin Update"
echo "═══════════════════════════════════════"
echo "Plugin dir: $PLUGIN_DIR"
echo ""

# Run the Python updater
python3 -m zulip.updater "$@"

echo ""
echo "═══════════════════════════════════════"
echo "  Restarting Hermes gateway..."
echo "═══════════════════════════════════════"

# Restart the gateway (handles both systemd and direct process)
if command -v systemctl &>/dev/null; then
    sudo systemctl restart hermes-gateway 2>/dev/null || sudo systemctl restart hermes 2>/dev/null || true
fi

# Fallback: try hermes CLI
hermes gateway restart 2>/dev/null || true

echo ""
echo "✅ Update complete. Gateway restarted."
