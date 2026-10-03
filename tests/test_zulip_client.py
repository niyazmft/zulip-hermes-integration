"""SDK state ownership after the layered refactor (Wave 2).

The ``zulip`` SDK handle and the ``ZULIP_AVAILABLE`` flag are owned by
``zulip.zulip_client``. If either were re-exported from ``zulip.adapter``, a
``monkeypatch.setattr(adapter, ...)`` site would silently stop taking effect
while the test kept passing — the exact silent-rot failure mode the wave
removes. These tests lock the ownership so a regression fails loudly, and
record the one invariant that makes the ``_target_cache`` memo safe.
"""

from __future__ import annotations

import sys
from pathlib import Path

import zulip.adapter as adapter_module
import zulip.zulip_client as zulip_client_module


def test_sdk_flag_and_handle_are_owned_by_zulip_client_only():
    assert hasattr(zulip_client_module, "ZULIP_AVAILABLE")
    assert hasattr(zulip_client_module, "zulip")
    # Deliberately not re-exported by the adapter, so a patch aimed at the
    # adapter raises instead of no-oping into a green-but-vacuous test.
    assert not hasattr(adapter_module, "ZULIP_AVAILABLE")
    assert not hasattr(adapter_module, "zulip")


def test_import_zulip_sdk_reads_the_owning_module(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)
    monkeypatch.setattr(zulip_client_module, "zulip", sentinel)
    assert zulip_client_module.import_zulip_sdk() is sentinel


def test_parse_target_is_a_pure_function_of_chat_id(monkeypatch):
    """The target memo is keyed on the chat_id alone.

    That is safe only because ``parse_target`` returns nothing profile- or
    account-dependent. If a profile-derived field were ever added, the cached
    entry could be served to another profile — so this pins both the result
    shape and the fact that the profile environment cannot change it.
    """
    monkeypatch.setenv("ZULIP_EMAIL", "alice@example.test")
    first = zulip_client_module.parse_target("dm:7,42")
    monkeypatch.setenv("ZULIP_EMAIL", "bob@example.test")
    second = zulip_client_module.parse_target("dm:7,42")

    assert first is second  # served from the memo
    assert first == {"type": "dm", "user_ids": [7, 42]}


def test_user_lookup_call_has_a_single_definition():
    from zulip import admin_actions

    assert admin_actions._user_lookup_call is zulip_client_module.user_lookup_call
    assert adapter_module._user_lookup_call is zulip_client_module.user_lookup_call


def test_import_zulip_sdk_prefers_a_real_sdk_over_this_plugin(
    tmp_path, monkeypatch
):
    """The shadow-bypass must actually bypass, not re-resolve back to us.

    This package is named ``zulip``, so a plain ``import zulip`` returns the
    plugin. The SDK here is placed *after* the plugin directory on ``sys.path``
    so naive resolution finds the plugin and only the path fix finds the SDK.
    """
    repo_root = Path(__file__).resolve().parent.parent
    sdk_dir = tmp_path / "fakesdk"
    (sdk_dir / "zulip").mkdir(parents=True)
    (sdk_dir / "zulip" / "__init__.py").write_text(
        "MARKER = 'real-sdk'\n", encoding="utf-8"
    )

    # Control sys.path EXACTLY: plugin dir first, fake SDK second, and NO
    # site-packages. CI installs the real SDK from requirements.txt, so leaving
    # site-packages on the path made this pass locally (SDK absent) and fail in
    # CI, where resolution continued past the plugin and found the real SDK.
    monkeypatch.setattr(sys, "path", [str(repo_root), str(sdk_dir)])
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", False)
    monkeypatch.setattr(zulip_client_module, "zulip", None)
    monkeypatch.delitem(sys.modules, "zulip", raising=False)

    sdk = zulip_client_module.import_zulip_sdk()

    assert sdk is not None, "the real SDK should have been found"
    assert getattr(sdk, "MARKER", None) == "real-sdk"
    assert not zulip_client_module._resolved_to_this_package(sdk)


def test_import_zulip_sdk_reports_absent_instead_of_returning_this_plugin(
    monkeypatch
):
    """With no SDK installed the plugin must never be handed back as the SDK."""
    # sys.path is replaced outright so no INSTALLED SDK can satisfy the import:
    # the point of this test is that a genuinely absent SDK is reported absent,
    # which is only testable if the environment cannot supply one. (CI installs
    # the real SDK, which is what broke the first version of this test.)
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.setattr(sys, "path", [str(repo_root)])
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", False)
    monkeypatch.setattr(zulip_client_module, "zulip", None)
    monkeypatch.delitem(sys.modules, "zulip", raising=False)

    assert zulip_client_module.import_zulip_sdk() is None
    assert zulip_client_module.ZULIP_AVAILABLE is False


def test_import_zulip_sdk_restores_sys_path_exactly(monkeypatch):
    """Path entries must return to their original indices, not just be re-added.

    ``sys.path`` order is import precedence, so a reorder is a real (if quiet)
    behaviour change for every later import.
    """
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.setattr(sys, "path", [str(repo_root), *sys.path])
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", False)
    monkeypatch.setattr(zulip_client_module, "zulip", None)
    before = list(sys.path)

    zulip_client_module.import_zulip_sdk()

    assert sys.path == before


def test_import_zulip_sdk_preserves_the_plugin_package_identity(monkeypatch):
    """After a failed SDK lookup, ``import zulip`` must yield the SAME plugin.

    Re-importing the package would mint fresh module globals -- a second
    ``_LIVE_ADAPTERS`` WeakSet and a second cache set -- which is exactly the
    kind of quiet divergence a later test would trip over.
    """
    import zulip as plugin_before

    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.setattr(sys, "path", [str(repo_root), *sys.path])
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", False)
    monkeypatch.setattr(zulip_client_module, "zulip", None)

    zulip_client_module.import_zulip_sdk()

    import zulip as plugin_after

    assert plugin_after is plugin_before
    assert sys.modules["zulip"] is plugin_before
