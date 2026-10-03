"""Composition-root pins for Wave 11 (final): the re-export surface.

Wave 11 finished ``zulip.adapter`` as a composition root by deleting the
re-export shims that had **zero** referents anywhere (no ``from
zulip.adapter import <name>`` / ``zulip.adapter.<name>`` / ``adapter_module.<name>``
reference, no internal use) and the dead delegate methods whose owning module is
reached directly by the caller.

A stale reference to any deleted name now raises ``AttributeError`` instead of
quietly no-oping — the same negative-control principle Waves 5/7/8 established.
Everything the host or a published import still reaches stays: ``register``,
``ZulipAdapter``, ``_LIVE_ADAPTERS``, ``_clear_caches``, ``_sdk_call``,
``_handle_message``, every ``BasePlatformAdapter`` override, and every
re-export that still has a referent.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import zulip.adapter as adapter_module

# Re-export shims deleted in Wave 11 (zero referents in zulip/, tests/,
# scripts/, .github/, *.md, *.yaml).
DELETED_SHIMS = (
    "CommandResult",
    "DEFAULT_CHUNK_LIMIT",
    "DEFAULT_CHUNK_MODE",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_MAX_MESSAGE_LENGTH",
    "DEFAULT_READ_TIMEOUT",
    "DEFAULT_SEND_TIMEOUT",
    "ENGAGEMENT_MODE_OFF",
    "HISTORY_FETCH_TIMEOUT",
    "MAX_INPUT_LENGTH",
    "OBSERVED_MAX_CHARS",
    "OBSERVED_MAX_MESSAGES",
    "OBSERVED_MAX_TOPICS",
    "ReactionLifecycle",
    "_get_cached_target",
    "_history_sort_key",
    "_joined_len",
    "_private_recipient_ids",
    "block_secret_leaks_enabled",
    "chunk_text",
    "collect_known_secrets",
    "create_mention_regex",
    "describe_leaked_secrets",
    "extract_topic_directive",
    "find_leaked_secrets",
    "format_zulip_log",
    "is_end_session_message",
    "is_stop_listening_message",
    "normalize_mention",
    "recover_interrupted_messages",
    "strip_onchar_prefix",
    "strip_think_blocks",
    "truncate_text",
    "__version__",
    "__repo__",
    # unused stdlib / gateway / module imports
    "time",
    "OrderedDict",
    "MessageType",
    "media",
    "refs",
    "commands",
    "connection",
    "inbound_queue",
    "updater",
)

# Dead delegate methods deleted in Wave 11 (owners reached directly).
DELETED_METHODS = (
    "_save_trace_records",
    "_drop_trace_record",
    "_audit_queue_transition",
    "_presence_heartbeat",
    "_detect_secret_leak",
    "_render_refs",
)


class TestDeletedShimsFailLoudly:
    def test_shims_are_gone_from_the_module(self):
        for name in DELETED_SHIMS:
            assert not hasattr(adapter_module, name), name

    def test_stale_attribute_access_raises(self):
        """The negative control: this is what a missed migration looks like."""
        for name in DELETED_SHIMS:
            with pytest.raises(AttributeError):
                getattr(adapter_module, name)

    def test_dead_delegates_are_gone_from_the_class(self):
        for name in DELETED_METHODS:
            assert not hasattr(adapter_module.ZulipAdapter, name), name


class TestKeptSurfaceStillResolves:
    def test_must_keep_module_names(self):
        for name in (
            "register",
            "ZulipAdapter",
            "_LIVE_ADAPTERS",
            "_clear_caches",
        ):
            assert hasattr(adapter_module, name), name

    def test_re_exports_with_referents_stay_resolvable(self):
        # A representative subset; each still has a published-import referent.
        for name in (
            "_message_with_flags",
            "_event_route",
            "_resolve_chatmode",
            "_standalone_send",
            "_clear_caches",
            "_split_session_key_tail",
            "_CONVERSATION_ID_RE",
            "_parse_target",
            "_user_lookup_call",
            "_safe_delete_temp_file",
            "_private_chat_id",
            "_resolve_max_message_length",
        ):
            assert hasattr(adapter_module, name), name

    def test_re_exports_still_reach_their_owner(self):
        import zulip.cli as cli_module
        import zulip.connection as connection_module
        import zulip.inbound_queue as inbound_queue_module
        import zulip.routing as routing_module
        import zulip.settings as settings_module

        assert adapter_module._message_with_flags is connection_module.message_with_flags
        assert adapter_module._event_route is inbound_queue_module.event_route
        assert adapter_module._resolve_chatmode is settings_module.resolve_chatmode
        assert adapter_module._standalone_send is cli_module.standalone_send
        assert adapter_module._split_session_key_tail is routing_module.split_session_key_tail


class TestBasePlatformAdapterContractUnchanged:
    CONTRACT = (
        "connect",
        "disconnect",
        "send",
        "send_typing",
        "stop_typing",
        "get_chat_info",
        "resolve_topic",
        "fetch_messages",
        "search_messages",
        "list_streams",
        "subscribe_stream",
        "delete_message",
        "get_user_presence",
        "star_message",
        "get_user_info",
        "send_image_file",
        "send_document",
        "on_processing_start",
        "on_processing_complete",
    )

    def test_every_contract_method_is_present_and_callable(self):
        for name in self.CONTRACT:
            method = getattr(adapter_module.ZulipAdapter, name, None)
            assert method is not None, name
            assert callable(method), name

    def test_internal_patch_surfaces_remain(self):
        # conftest clears _LIVE_ADAPTERS; tests patch the _sdk_call instance
        # method; the event loop and recovery path call _handle_message.
        assert callable(adapter_module.ZulipAdapter._sdk_call)
        assert callable(adapter_module.ZulipAdapter._handle_message)
        assert callable(adapter_module._clear_caches)


class TestRegisterKwargsUnchanged:
    def _register_kwargs(self):
        calls = []
        ctx = SimpleNamespace(
            register_platform=lambda **kw: calls.append(kw),
            register_hook=lambda *a, **k: None,
            register_tool=lambda **kw: None,
        )
        adapter_module.register(ctx)
        assert len(calls) == 1
        return calls[0]

    def test_exact_kwarg_set(self):
        assert set(self._register_kwargs()) == {
            "name",
            "label",
            "adapter_factory",
            "check_fn",
            "validate_config",
            "required_env",
            "install_hint",
            "env_enablement_fn",
            "allowed_users_env",
            "allow_all_env",
            "cron_deliver_env_var",
            "standalone_sender_fn",
            "max_message_length",
            "platform_hint",
            "emoji",
            "setup_fn",
        }

    def test_plain_values_unchanged(self):
        kw = self._register_kwargs()
        assert kw["name"] == "zulip"
        assert kw["label"] == "Zulip"
        assert kw["required_env"] == ["ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"]
        assert kw["install_hint"] == "pip install zulip"
        assert kw["allowed_users_env"] == "ZULIP_ALLOWED_USERS"
        assert kw["allow_all_env"] == "ZULIP_ALLOW_ALL_USERS"
        assert kw["cron_deliver_env_var"] == "ZULIP_HOME_CHANNEL"
        assert kw["max_message_length"] == 10000
        assert kw["emoji"] == "📬"
        assert "Zulip" in kw["platform_hint"]

    def test_callables_are_the_live_objects(self):
        import zulip.cli as cli_module

        kw = self._register_kwargs()
        assert kw["check_fn"] is cli_module.check_requirements
        assert kw["validate_config"] is cli_module.validate_config
        assert kw["env_enablement_fn"] is cli_module.env_enablement
        assert kw["setup_fn"] is cli_module.interactive_setup
        assert kw["standalone_sender_fn"] is cli_module.standalone_send
        assert inspect.iscoroutinefunction(kw["standalone_sender_fn"])


class TestPackageEntryPoint:
    def test_init_exports_register(self):
        import zulip

        assert zulip.register is adapter_module.register

    def test_init_imports_cleanly(self):
        import importlib

        module = importlib.import_module("zulip")
        assert callable(module.register)
