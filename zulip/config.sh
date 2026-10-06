#!/usr/bin/env bash
# config.sh — Inspect or change the Zulip plugin's effective configuration.
#
# Usage:
#   bash ~/.hermes/plugins/zulip/config.sh             # effective configuration
#   bash ~/.hermes/plugins/zulip/config.sh --advanced  # also the plumbing knobs
#   bash ~/.hermes/plugins/zulip/config.sh --wizard    # edit the curated settings
#
# Hermes exposes no `plugins config` subcommand, so this shim is the documented
# entry point, sitting beside update.sh (epic #211, child #216).

set -euo pipefail

# This script lives *inside* the package (``zulip/config.sh``), because that is
# where the updater deploys it and where the README's documented path points --
# the same reasoning that moved update.sh here in #205/#208.
PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT_DIR="$(dirname "$PKG_DIR")"

# cli.py uses relative imports, so it has to run as a module with the package
# parent on sys.path; running it as a file fails outright.
#
# Importing the package also runs zulip/__init__.py, which imports the adapter,
# which imports the Hermes host (`gateway`). A bare `python3` therefore fails with
# "No module named 'gateway'". So this needs the interpreter the gateway itself
# runs under -- discovered from the `hermes` launcher's shebang, or set explicitly.
PY="${ZULIP_PYTHON:-}"
if [ -z "$PY" ] && command -v hermes >/dev/null 2>&1; then
    shebang="$(head -1 "$(command -v hermes)" 2>/dev/null || true)"
    case "$shebang" in
        '#!'*) PY="${shebang#\#!}" ;;
    esac
fi
PY="${PY:-python3}"

cd "$PARENT_DIR"
if ! "$PY" -c 'import zulip' >/dev/null 2>&1; then
    echo "zulip: '${PY}' cannot import the plugin." >&2
    echo "       The Hermes host module 'gateway' must be importable, so run this" >&2
    echo "       with the same Python that runs the gateway, or point" >&2
    echo "       ZULIP_PYTHON at it, e.g.:" >&2
    echo "         ZULIP_PYTHON=/path/to/gateway/python bash $0 $*" >&2
    exit 1
fi

exec "$PY" -m zulip.cli config "$@"
