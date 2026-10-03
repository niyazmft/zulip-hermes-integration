"""Patch-ownership pins for the Wave 7 extraction.

The rule (learned the hard way in Wave 5): the OWNING module is the module that
DEFINES a name, not the module that calls it, and the production read and the
test patch must point at the SAME module. A ``monkeypatch.setattr`` aimed at the
caller — ``zulip.adapter`` — otherwise leaves the test green while exercising
production: a silently dead test.

Wave 7 moved the per-session queue orchestration into ``zulip.inbound_queue``
and the connection / event-loop lifecycle into ``zulip.connection``; ``connect``
and ``disconnect`` remain ``BasePlatformAdapter`` overrides on the adapter with
unchanged signatures. ``probe_zulip`` (owned by ``zulip.probe``) and
``handle_command`` (owned by ``zulip.commands``) move their read sites to the
owning module, so the old adapter targets are gone. These tests fail loudly if
any of that regresses.
"""

from __future__ import annotations

import inspect


class TestStaleCallerTargetsAreGone:
    def test_probe_zulip_is_not_reachable_through_adapter(self):
        import zulip.adapter as adapter_module

        assert not hasattr(adapter_module, "probe_zulip")

    def test_handle_command_is_not_reachable_through_adapter(self):
        import zulip.adapter as adapter_module

        assert not hasattr(adapter_module, "handle_command")


class TestOwnerModulesDefineTheNames:
    def test_probe_owns_probe_zulip(self):
        import zulip.probe as probe_module

        assert probe_module.probe_zulip.__module__ == "zulip.probe"

    def test_commands_owns_handle_command(self):
        import zulip.commands as commands_module

        assert commands_module.handle_command.__module__ == "zulip.commands"


class TestMovedHelpersStayImportableFromAdapter:
    def test_message_with_flags_re_exported_from_adapter(self):
        from zulip.adapter import _message_with_flags

        assert _message_with_flags.__module__ == "zulip.connection"

    def test_event_route_re_exported_from_adapter(self):
        from zulip.adapter import _event_route

        assert _event_route.__module__ == "zulip.inbound_queue"


class TestBasePlatformAdapterContractUnchanged:
    def test_connect_signature_unchanged(self):
        from zulip.adapter import ZulipAdapter

        sig = inspect.signature(ZulipAdapter.connect)
        assert list(sig.parameters) == ["self", "is_reconnect"]
        param = sig.parameters["is_reconnect"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is False

    def test_disconnect_signature_unchanged(self):
        from zulip.adapter import ZulipAdapter

        assert list(inspect.signature(ZulipAdapter.disconnect).parameters) == ["self"]


class TestControllersOwnTheLifecycle:
    def test_connection_controller_owns_connect_and_listen(self):
        from zulip.connection import ZulipConnection

        for name in (
            "connect",
            "disconnect",
            "needed_event_types",
            "register_queue",
            "presence_heartbeat",
            "listen_for_events",
        ):
            assert callable(getattr(ZulipConnection, name)), name

    def test_queue_controller_owns_the_queue_orchestration(self):
        from zulip.inbound_queue import SessionQueueController

        for name in (
            "queue_session_key",
            "audit_queue_transition",
            "_queue_turn",
            "_drain_session_queue",
            "_dispatch_turn",
        ):
            assert callable(getattr(SessionQueueController, name)), name
