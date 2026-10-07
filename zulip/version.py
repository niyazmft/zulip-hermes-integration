"""Version and update metadata for the Zulip Hermes plugin.

This module is the single source of truth for the plugin version.
When releasing, bump __version__ and create a matching Git tag.
"""

__version__ = "1.12.0"
__repo__ = "niyazmft/zulip-hermes-integration"
__min_hermes__ = "0.18.2"

# Files that make up the plugin — used by self-updater
PLUGIN_FILES = [
    "__init__.py",
    "adapter.py",
    "admin_actions.py",
    "approval_outcomes.py",
    "approvals.py",
    "audit_logger.py",
    "cli.py",
    "commands.py",
    "config.sh",
    "connection.py",
    "conversations.py",
    "activity_trace.py",
    "dedupe_store.py",
    "display_names.py",
    "engagement.py",
    "fallback_reader.py",
    "history.py",
    "inbound.py",
    "inbound_queue.py",
    "logger.py",
    "media.py",
    "outbound.py",
    "pairing.py",
    "platform_api.py",
    "plugin.yaml",
    "policy.py",
    "probe.py",
    "queue_manager.py",
    "rate_limiter.py",
    "reaction_triggers.py",
    "reactions.py",
    "recovery.py",
    "refs.py",
    "routing.py",
    "runtime_scope.py",
    "secret_guard.py",
    "session_queue.py",
    "settings.py",
    "text_utils.py",
    "tracing.py",
    "update.sh",
    "updater.py",
    "version.py",
    "workspace.py",
    "zulip_client.py",
]
