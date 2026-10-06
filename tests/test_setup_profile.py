"""One recommended-setup question instead of the prompt wall (epic #211, #215).

``zulip/cli.py::interactive_setup`` used to walk the declared settings, which
for a 54-knob manifest is a wall of prompts for someone who just wants a working
bot.  It now asks the three credentials, then **one** question.

The acceptance criterion is exact: answering yes writes **exactly** the four
values -- ``ZULIP_SITE``, ``ZULIP_EMAIL``, ``ZULIP_API_KEY`` and
``ZULIP_PROFILE=recommended`` -- and prompts for nothing else.  The tests below
assert the saved dict, not just that setup ran, because a stray extra write is
precisely the failure mode being fixed.

``hermes_cli.setup`` is injected as a fake module: the real one is part of the
Hermes host, and the plugin deliberately lazy-imports it so this file is the
only thing that needs to stand in for it.
"""

from __future__ import annotations

import sys
import types

import pytest

from zulip import cli


class FakeHermesSetup:
    """Minimal stand-in for ``hermes_cli.setup`` that records what was asked."""

    def __init__(self, *, answers=(), yes_no=(), env=None):
        self.answers = list(answers)
        self.yes_no = list(yes_no)
        self.env = dict(env or {})
        self.saved: dict[str, str] = {}
        self.questions: list[tuple[str, str]] = []
        self.messages: list[tuple[str, str]] = []

    # -- the surface interactive_setup imports -----------------------------

    def prompt(self, question, default="", password=False):
        self.questions.append(("prompt", question))
        return self.answers.pop(0) if self.answers else default

    def prompt_yes_no(self, question, default=False):
        self.questions.append(("yes_no", question))
        return self.yes_no.pop(0) if self.yes_no else default

    def save_env_value(self, name, value):
        self.saved[name] = value

    def get_env_value(self, name):
        return self.env.get(name)

    def _print(self, kind):
        return lambda text="": self.messages.append((kind, text))

    def install(self, monkeypatch):
        module = types.ModuleType("hermes_cli.setup")
        module.prompt = self.prompt
        module.prompt_yes_no = self.prompt_yes_no
        module.save_env_value = self.save_env_value
        module.get_env_value = self.get_env_value
        module.print_header = self._print("header")
        module.print_info = self._print("info")
        module.print_warning = self._print("warning")
        module.print_success = self._print("success")
        package = types.ModuleType("hermes_cli")
        package.setup = module
        monkeypatch.setitem(sys.modules, "hermes_cli", package)
        monkeypatch.setitem(sys.modules, "hermes_cli.setup", module)
        return self

    def asked(self) -> str:
        return "\n".join(question for _kind, question in self.questions)

    def said(self) -> str:
        return "\n".join(text for _kind, text in self.messages)

    def of_kind(self, kind: str) -> list[str]:
        return [text for entry_kind, text in self.messages if entry_kind == kind]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "ZULIP_ALLOW_INSECURE_HTTP",
        "ZULIP_PROFILE",
        "ZULIP_ALLOWED_USERS",
    ):
        monkeypatch.delenv(name, raising=False)


CREDENTIALS = ("https://test.zulipchat.com", "bot@test.zulipchat.com", "secret-key")


def _setup(monkeypatch, *, answers=CREDENTIALS, yes_no=(True,), env=None):
    return FakeHermesSetup(answers=answers, yes_no=yes_no, env=env).install(monkeypatch)


# ----------------------------------------------------------- the yes path


def test_yes_writes_exactly_the_four_values(monkeypatch):
    fake = _setup(monkeypatch)

    cli.interactive_setup()

    assert fake.saved == {
        "ZULIP_SITE": "https://test.zulipchat.com",
        "ZULIP_EMAIL": "bot@test.zulipchat.com",
        "ZULIP_API_KEY": "secret-key",
        "ZULIP_PROFILE": "recommended",
    }


def test_yes_prompts_for_nothing_else(monkeypatch):
    """Three credentials and one question. No "Allowed user emails", no walk."""
    fake = _setup(monkeypatch)

    cli.interactive_setup()

    assert len(fake.questions) == 4
    assert fake.questions[3] == ("yes_no", "Use the recommended setup?")
    assert "Allowed user emails" not in fake.asked()
    assert "ZULIP_ALLOWED_USERS" not in fake.saved


def test_the_recommended_question_defaults_to_yes(monkeypatch):
    """Pressing enter must choose the recommended setup, not the long walk."""
    fake = FakeHermesSetup(answers=CREDENTIALS).install(monkeypatch)  # no yes_no answers

    cli.interactive_setup()

    assert fake.saved["ZULIP_PROFILE"] == "recommended"


def test_yes_is_idempotent(monkeypatch):
    fake = _setup(monkeypatch)
    cli.interactive_setup()
    first = dict(fake.saved)

    fake.answers = list(CREDENTIALS)
    fake.yes_no = [True]
    cli.interactive_setup()

    assert fake.saved == first


def test_the_subscribe_tip_survives(monkeypatch):
    """Load-bearing: an unsubscribed bot sees no messages at all."""
    fake = _setup(monkeypatch)

    cli.interactive_setup()

    assert "Subscribe your bot to streams" in fake.said()
    assert fake.of_kind("success")


def test_the_marker_uses_the_shared_constant(monkeypatch):
    """The written value is the name runtime_scope actually recognises."""
    from zulip import runtime_scope

    fake = _setup(monkeypatch)
    cli.interactive_setup()

    assert fake.saved["ZULIP_PROFILE"] == runtime_scope.PROFILE_RECOMMENDED
    assert runtime_scope.PROFILE_RECOMMENDED in runtime_scope.KNOWN_PROFILES


# ------------------------------------------------------------ the no path


def test_no_keeps_the_longer_walk_and_writes_no_marker(monkeypatch):
    fake = _setup(monkeypatch, yes_no=(False,), answers=(*CREDENTIALS, "alice@example.com"))

    cli.interactive_setup()

    assert "ZULIP_PROFILE" not in fake.saved
    assert fake.saved["ZULIP_ALLOWED_USERS"] == "alice@example.com"
    assert "Allowed user emails" in fake.asked()


# ------------------------------------------------------ unchanged behaviour


def test_missing_site_still_aborts(monkeypatch):
    fake = _setup(monkeypatch, answers=("",))

    cli.interactive_setup()

    assert fake.saved == {}
    assert any("required" in text for text in fake.of_kind("warning"))


def test_missing_api_key_still_aborts(monkeypatch):
    """Aborting on a missing key must not write the marker.

    Credentials are saved step-by-step, so site and email land before the key is
    asked for -- pre-existing behaviour this child does not change.  What must
    hold is that an aborted run never writes ``ZULIP_PROFILE``: a marker without
    a bot that can connect would be a profile pointing at nothing.
    """
    fake = _setup(monkeypatch, answers=(*CREDENTIALS[:2], ""))

    cli.interactive_setup()

    assert "ZULIP_API_KEY" not in fake.saved
    assert "ZULIP_PROFILE" not in fake.saved
    assert any("required" in text for text in fake.of_kind("warning"))


def test_http_site_is_still_refused_without_the_opt_in(monkeypatch):
    """Issue #137: the API key travels in cleartext over plain http."""
    fake = _setup(monkeypatch, answers=("http://internal.example.com",))

    cli.interactive_setup()

    assert fake.saved == {}
    assert fake.of_kind("warning")


def test_reconfigure_declined_still_returns_early(monkeypatch):
    fake = _setup(
        monkeypatch,
        env={"ZULIP_EMAIL": "bot@test.zulipchat.com"},
        yes_no=(False,),
    )

    cli.interactive_setup()

    assert fake.saved == {}
    assert fake.questions == [("yes_no", "Reconfigure Zulip?")]
