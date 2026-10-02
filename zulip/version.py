"""Version and update metadata for the Zulip Hermes plugin.

This module is the single source of truth for the plugin version.
When releasing, bump __version__ and create a matching Git tag.
"""

__version__ = "1.10.1"
__repo__ = "niyazmft/zulip-hermes-integration"
__min_hermes__ = "0.18.2"

# Files that make up the plugin — used by self-updater
PLUGIN_FILES = [
    "__init__.py",
    "accounts.py",
    "adapter.py",
    "admin_actions.py",
    "audit_logger.py",
    "commands.py",
    "conversations.py",
    "activity_trace.py",
    "dedupe_store.py",
    "display_names.py",
    "engagement.py",
    "fallback_reader.py",
    "logger.py",
    "media.py",
    "pairing.py",
    "plugin.yaml",
    "policy.py",
    "probe.py",
    "queue_manager.py",
    "rate_limiter.py",
    "reaction_triggers.py",
    "reactions.py",
    "recovery.py",
    "refs.py",
    "runtime_scope.py",
    "secret_guard.py",
    "session_queue.py",
    "text_utils.py",
    "update.sh",
    "updater.py",
    "version.py",
    "workspace.py",
]
