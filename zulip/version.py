"""Version and update metadata for the Zulip Hermes plugin.

This module is the single source of truth for the plugin version.
When releasing, bump __version__ and create a matching Git tag.
"""

__version__ = "1.5.0"
__repo__ = "niyazmft/zulip-hermes-integration"
__min_hermes__ = "0.18.2"

# Files that make up the plugin — used by self-updater
PLUGIN_FILES = [
    "__init__.py",
    "adapter.py",
    "dedupe_store.py",
    "logger.py",
    "media.py",
    "plugin.yaml",
    "queue_manager.py",
    "reactions.py",
    "text_utils.py",
    "version.py",
    "workspace.py",
]
