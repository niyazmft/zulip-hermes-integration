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

# Run the Python updater as a SCRIPT, not `-m zulip.updater`.
#
# The -m form imports the zulip package, and zulip/__init__.py imports the
# adapter -- which is precisely what fails on a tree that a partial update has
# already broken. Running updater.py directly skips the package import, so the
# updater can still repair an install that no longer loads (issue #204).
# updater.py deliberately has no relative imports so this works.
python3 "$PLUGIN_DIR/updater.py" "$@"

echo ""
echo "═══════════════════════════════════════"
echo "  Restarting Hermes gateway..."
echo "═══════════════════════════════════════"

# Restarting is environment-specific -- systemd, pm2, s6, or a bare process --
# so we attempt the known mechanisms and then CHECK that the process was
# actually replaced.
#
# Announcing a restart we did not verify is worse than asking the operator to do
# it: the new code loads only when the gateway process restarts, so a silent
# no-op leaves the old code running while the user believes the update is live.
# The previous version of this script swallowed every failure with `|| true`
# and then printed "Gateway restarted" unconditionally.
gateway_pid_before="$(pgrep -f 'hermes gateway' 2>/dev/null | head -1 || true)"

if command -v systemctl >/dev/null 2>&1 \
   && systemctl list-unit-files 2>/dev/null | grep -qE '^hermes(-gateway)?\.service'; then
    sudo systemctl restart hermes-gateway 2>/dev/null \
        || sudo systemctl restart hermes 2>/dev/null || true
elif command -v pm2 >/dev/null 2>&1 \
   && pm2 jlist 2>/dev/null | grep -q '"name":"hermes"'; then
    pm2 restart hermes >/dev/null 2>&1 || true
elif command -v hermes >/dev/null 2>&1; then
    hermes gateway restart >/dev/null 2>&1 || true
fi

# Verify: the gateway process must have been replaced.
restarted="no"
gateway_pid_after=""
if [ -n "$gateway_pid_before" ]; then
    for _ in $(seq 1 20); do
        sleep 1
        gateway_pid_after="$(pgrep -f 'hermes gateway' 2>/dev/null | head -1 || true)"
        if [ -n "$gateway_pid_after" ] && [ "$gateway_pid_after" != "$gateway_pid_before" ]; then
            restarted="yes"
            break
        fi
    done
fi

echo ""
if [ "$restarted" = "yes" ]; then
    echo "✅ Update complete. Gateway restarted (pid $gateway_pid_before → $gateway_pid_after)."
else
    echo "⚠️  Files updated, but the gateway was NOT restarted (or the restart could not be confirmed)."
    echo ""
    echo "   The new code loads only when the gateway process restarts."
    echo "   Restart it now, choosing what fits this host:"
    echo ""
    echo "     systemd  sudo systemctl restart hermes-gateway"
    echo "     pm2      pm2 restart hermes"
    echo "     s6       s6-svc -r /run/service/gateway-default"
    echo "     manual   hermes gateway restart"
fi
