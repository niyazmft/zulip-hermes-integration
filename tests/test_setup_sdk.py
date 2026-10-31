"""The Zulip SDK: one probe, one consented install, and the boundaries around it.

The plugin cannot sign in or send a message without the SDK. ``check_requirements()``
reports that to Hermes, which correctly refuses to enable the plugin — but a
first-time user cannot act on a report from inside setup. This module covers the
repair path and, just as importantly, the rules that keep it safe:

* never at import — a plugin that pip-installs when it loads mutates the
  environment the *host* runs from,
* never silently — the question is asked, and declining prints the exact command,
* never forced — a failed install warns and setup continues.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import types

import pytest

from zulip import cli


class Prompt:
    """Records the question it was asked and answers with a fixed value."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, question: str, default: bool = False) -> bool:
        self.calls.append((question, default))
        return self.answer


class Printer:
    """Stands in for the four ``hermes_cli.setup`` printers."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _record(self, kind: str):
        def record(text: str = "") -> None:
            self.lines.append((kind, text))

        return record

    def install(self) -> "Printer":
        self.info = self._record("info")
        self.warning = self._record("warning")
        self.success = self._record("success")
        return self

    def text(self) -> str:
        return "\n".join(text for _kind, text in self.lines)


@pytest.fixture
def sdk_absent(monkeypatch):
    monkeypatch.setattr(cli, "_zulip_sdk_available", lambda: False)


def _ensure(answer: bool):
    printer = Printer().install()
    prompt = Prompt(answer)
    ok = cli.ensure_zulip_sdk(
        prompt_yes_no=prompt,
        print_info=printer.info,
        print_warning=printer.warning,
        print_success=printer.success,
    )
    return ok, prompt, printer


def test_no_offer_when_the_sdk_is_already_importable(monkeypatch):
    monkeypatch.setattr(cli, "_zulip_sdk_available", lambda: True)

    def _must_not_install():
        raise AssertionError("an installed SDK must not trigger an install")

    monkeypatch.setattr(cli, "install_zulip_sdk", _must_not_install)
    ok, prompt, printer = _ensure(answer=False)

    assert ok is True
    assert prompt.calls == [], "an installed SDK must not be mentioned"
    assert printer.lines == []


def test_declining_installs_nothing_and_prints_the_exact_command(
    sdk_absent, monkeypatch
):
    def _must_not_install():
        raise AssertionError("declining must not run pip")

    monkeypatch.setattr(cli, "install_zulip_sdk", _must_not_install)
    ok, prompt, printer = _ensure(answer=False)

    assert ok is False
    assert len(prompt.calls) == 1
    assert prompt.calls[0][1] is True, "the offer defaults to yes"
    text = printer.text()
    assert f"{sys.executable} -m pip install" in text
    assert cli.ZULIP_SDK_REQUIREMENT in text


def test_accepting_installs_once_and_reports_success(monkeypatch):
    state = {"available": False}
    monkeypatch.setattr(cli, "_zulip_sdk_available", lambda: state["available"])
    calls: list[int] = []

    def fake_install():
        calls.append(1)
        state["available"] = True  # pip succeeded
        return True, ""

    monkeypatch.setattr(cli, "install_zulip_sdk", fake_install)
    ok, prompt, printer = _ensure(answer=True)

    assert ok is True
    assert calls == [1], "exactly one install attempt"
    assert "installed" in printer.text().lower()


def test_a_successful_pip_that_still_cannot_import_is_a_warning(
    sdk_absent, monkeypatch
):
    """``pip`` returning 0 is not proof: the wheel may be in another interpreter."""
    monkeypatch.setattr(cli, "install_zulip_sdk", lambda: (True, "Successfully installed zulip"))
    ok, _prompt, printer = _ensure(answer=True)

    assert ok is False
    assert "still not importable" in printer.text()
    assert cli.ZULIP_SDK_REQUIREMENT in printer.text()


def test_a_failed_install_is_reported_not_raised(sdk_absent, monkeypatch):
    monkeypatch.setattr(
        cli, "install_zulip_sdk", lambda: (False, "ERROR: could not find a version")
    )
    ok, _prompt, printer = _ensure(answer=True)

    assert ok is False
    assert "ERROR: could not find a version" in printer.text()


def test_install_targets_the_running_interpreter(monkeypatch):
    """The gateway's interpreter, not whatever ``python3`` resolves to on PATH."""
    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    ok, output = cli.install_zulip_sdk()

    assert ok is True
    assert captured["command"] == [
        sys.executable,
        "-m",
        "pip",
        "install",
        cli.ZULIP_SDK_REQUIREMENT,
    ]
    assert captured["kwargs"]["timeout"] == cli.SDK_INSTALL_TIMEOUT_SECONDS
    assert captured["kwargs"]["capture_output"] is True


def test_a_missing_pip_is_reported_not_raised(monkeypatch):
    def fake_run(command, **kwargs):
        raise FileNotFoundError("pip not found")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    ok, output = cli.install_zulip_sdk()

    assert ok is False
    assert "pip not found" in output


def test_the_install_has_exactly_one_call_site():
    """Never at import: an install must always sit behind the consent prompt.

    A plugin that pip-installs when it loads mutates the environment the host runs
    from, needs network at deploy time, and fails into a startup log nobody reads.
    This asserts the call graph stays one deep: the definition, and the single call
    inside :func:`ensure_zulip_sdk`.
    """
    source = pathlib.Path(cli.__file__).read_text(encoding="utf-8")
    calls = [line for line in source.splitlines() if "install_zulip_sdk(" in line]
    assert len(calls) == 2, (
        "install_zulip_sdk must only be called from ensure_zulip_sdk; found:\n"
        + "\n".join(calls)
    )


# --------------------------------------------------------------- the wiring


def _fake_hermes_setup(monkeypatch, *, answers):
    """Minimal ``hermes_cli.setup`` so ``interactive_setup`` can run."""
    module = types.ModuleType("hermes_cli.setup")
    remaining = list(answers)

    module.prompt = lambda question, default="", password=False: (
        remaining.pop(0) if remaining else default
    )
    module.prompt_yes_no = lambda question, default=False: True
    module.save_env_value = lambda name, value: None
    module.get_env_value = lambda name: None
    for name in ("print_header", "print_info", "print_warning", "print_success"):
        setattr(module, name, lambda text="": None)

    package = types.ModuleType("hermes_cli")
    package.setup = module
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.setup", module)


def test_setup_offers_the_install_with_the_host_printers(monkeypatch):
    """Wiring, not behaviour: the offer happens during setup, with what setup has."""
    seen: dict[str, object] = {}

    def fake_ensure(**kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(cli, "ensure_zulip_sdk", fake_ensure)
    _fake_hermes_setup(
        monkeypatch,
        answers=["https://test.zulipchat.com", "bot@test.zulipchat.com", "key"],
    )

    cli.interactive_setup()

    assert seen, "setup must offer the SDK install"
    assert set(seen) == {
        "prompt_yes_no",
        "print_info",
        "print_warning",
        "print_success",
    }


def test_the_requirement_matches_requirements_txt():
    """One version to maintain, not two.

    ``ZULIP_SDK_REQUIREMENT`` is a second copy of the line in ``requirements.txt``.
    A copy that nobody ties down is how a plugin ends up installing a version its
    own requirements file does not name.
    """
    requirements = (
        pathlib.Path(cli.__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8")
    assert cli.ZULIP_SDK_REQUIREMENT in requirements
