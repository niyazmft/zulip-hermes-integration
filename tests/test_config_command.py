"""``zulip config`` — the effective view and the curated wizard (epic #211, #216).

The command exists to answer "what did it decide for me?", so the tests here
assert **provenance**, not just values: a knob must report `env` when it was set
explicitly, `profile` when ``ZULIP_PROFILE=recommended`` supplied it, and
`default` when nothing did.

The wizard's contract is the interesting one. It must write **only deviations**:
answering with the value the profile already supplies has to leave ``.env``
untouched (or remove a redundant entry), never duplicate the profile into the
file. That is what keeps ``.env`` readable and the profile meaningful.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from zulip import cli, runtime_scope, settings

CURATED_NAMES = {knob.name for knob in cli.CURATED_KNOBS}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for name in (*CURATED_NAMES, "ZULIP_PROFILE", "ZULIP_SOFT_GATE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def recommended(monkeypatch):
    monkeypatch.setenv("ZULIP_PROFILE", "recommended")


# ------------------------------------------------------------- provenance


def test_no_profile_reports_the_built_in_default():
    rows = {row.name: row for row in cli.effective_config()}
    assert rows["ZULIP_CHATMODE"].value == "onmessage"
    assert rows["ZULIP_CHATMODE"].source == runtime_scope.SOURCE_DEFAULT


def test_the_profile_is_reported_as_the_source(recommended):
    rows = {row.name: row for row in cli.effective_config()}
    assert rows["ZULIP_CHATMODE"].value == "oncall"
    assert rows["ZULIP_CHATMODE"].source == runtime_scope.SOURCE_PROFILE
    assert rows["ZULIP_DM_POLICY"].value == "allowlist"
    assert rows["ZULIP_DM_POLICY"].source == runtime_scope.SOURCE_PROFILE


def test_an_explicit_value_is_reported_as_env(recommended, monkeypatch):
    monkeypatch.setenv("ZULIP_HISTORY_MODE", "always")
    rows = {row.name: row for row in cli.effective_config()}
    assert rows["ZULIP_HISTORY_MODE"].value == "always"
    assert rows["ZULIP_HISTORY_MODE"].source == runtime_scope.SOURCE_ENV


def test_every_curated_knob_reports_something():
    """A knob with no row would silently drop out of the view."""
    rows = {row.name for row in cli.effective_config()}
    assert CURATED_NAMES <= rows


def test_the_advanced_view_adds_the_plumbing_knobs(recommended):
    curated_only = cli.effective_config()
    advanced = cli.effective_config(advanced=True)
    assert len(advanced) > len(curated_only)
    # The curated knobs must not be listed twice.
    names = [row.name for row in advanced]
    assert len(names) == len(set(names))
    assert CURATED_NAMES <= set(names)
    # ...and the advanced tier really does reach the manifest surface.
    declared = {name for name, _ in cli.declared_settings()}
    assert set(names) <= declared
    assert len(declared) == len(names)


def test_every_knob_the_plugin_reads_is_in_the_advanced_view():
    """The manifest is the contract; the view must cover all of it."""
    declared = dict(cli.declared_settings())
    assert {"ZULIP_API_KEY", "ZULIP_SITE", "ZULIP_EMAIL"} <= set(declared)
    assert {"ZULIP_SOFT_GATE", "ZULIP_PROFILE"} <= set(declared)


# ---------------------------------------------------- the mutual exclusion


def test_the_conflict_warning_matches_the_adapters_wording(recommended):
    """One string, two consumers: the CLI and the adapter's startup warning."""
    assert cli.soft_gate_observe_conflict() is False  # the preset picks observe only
    text = cli.render_config()
    assert settings.SOFT_GATE_OBSERVE_CONFLICT not in text

    import os

    os.environ["ZULIP_SOFT_GATE"] = "true"
    try:
        assert cli.soft_gate_observe_conflict() is True
        assert settings.SOFT_GATE_OBSERVE_CONFLICT in cli.render_config()
    finally:
        del os.environ["ZULIP_SOFT_GATE"]


def test_the_adapter_logs_the_shared_constant():
    """The adapter must not carry its own copy of the explanation (#216)."""
    source = (
        Path(__file__).resolve().parent.parent / "zulip" / "adapter.py"
    ).read_text(encoding="utf-8")
    assert "_SOFT_GATE_OBSERVE_CONFLICT" in source
    # The old inline literal is gone; only the shared constant remains.
    assert '"ZULIP_SOFT_GATE and ZULIP_OBSERVE_GROUP are both enabled "' not in source


# ----------------------------------------------------- baseline semantics


def test_baseline_follows_the_profile_when_one_is_active(recommended):
    knob = next(k for k in cli.CURATED_KNOBS if k.name == "ZULIP_HISTORY_MODE")
    assert knob.default == "off"
    assert cli.baseline_value(knob) == "on-demand"  # the profile's value


def test_baseline_is_the_built_in_default_without_a_profile():
    knob = next(k for k in cli.CURATED_KNOBS if k.name == "ZULIP_HISTORY_MODE")
    assert cli.baseline_value(knob) == "off"


# ------------------------------------------------------------- the wizard


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_wizard_writes_only_the_deviation(tmp_path, recommended):
    env = tmp_path / ".env"
    _write(env, "# kept\nZULIP_API_KEY=keepme\n")

    changes = cli.run_config_wizard(
        env_path=env,
        prompter=lambda knob, current, source: {"ZULIP_HISTORY_MODE": "always"}.get(
            knob.name
        ),
    )

    assert changes == ["ZULIP_HISTORY_MODE=always"]
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# kept\nZULIP_API_KEY=keepme")  # untouched lines survive
    assert "ZULIP_HISTORY_MODE=always" in text
    assert text.count("ZULIP_HISTORY_MODE") == 1


def test_wizard_does_not_restate_the_baseline(tmp_path, recommended):
    """Answering with the profile's own value must not write anything."""
    env = tmp_path / ".env"
    _write(env, "ZULIP_API_KEY=keepme\n")

    changes = cli.run_config_wizard(
        env_path=env,
        prompter=lambda knob, current, source: {"ZULIP_CHATMODE": "oncall"}.get(
            knob.name
        ),
    )

    assert changes == []
    assert env.read_text(encoding="utf-8") == "ZULIP_API_KEY=keepme\n"


def test_wizard_removes_a_deprecated_deviation(tmp_path, recommended):
    """Returning to the baseline removes the entry rather than writing it out."""
    env = tmp_path / ".env"
    _write(env, "ZULIP_API_KEY=keepme\nZULIP_CHATMODE=onmessage\n")

    changes = cli.run_config_wizard(
        env_path=env,
        prompter=lambda knob, current, source: {"ZULIP_CHATMODE": "oncall"}.get(
            knob.name
        ),
    )

    assert changes == ["ZULIP_CHATMODE -> back to the baseline (removed)"]
    text = env.read_text(encoding="utf-8")
    assert "ZULIP_CHATMODE" not in text
    assert "ZULIP_API_KEY=keepme" in text


def test_wizard_keeps_everything_on_enter(tmp_path, recommended):
    env = tmp_path / ".env"
    _write(env, "ZULIP_API_KEY=keepme\n")

    changes = cli.run_config_wizard(env_path=env, prompter=lambda *_: None)

    assert changes == []
    assert env.read_text(encoding="utf-8") == "ZULIP_API_KEY=keepme\n"


def test_wizard_creates_the_file_when_a_deviation_appears(tmp_path, recommended):
    env = tmp_path / "nested" / ".env"
    changes = cli.run_config_wizard(
        env_path=env,
        prompter=lambda knob, current, source: {"ZULIP_ACTIVITY_TRACE": "false"}.get(
            knob.name
        ),
    )
    assert changes == ["ZULIP_ACTIVITY_TRACE=false"]
    assert "ZULIP_ACTIVITY_TRACE=false" in env.read_text(encoding="utf-8")


def test_wizard_writes_owner_only(tmp_path, recommended):
    env = tmp_path / ".env"
    cli.run_config_wizard(
        env_path=env,
        prompter=lambda knob, current, source: {"ZULIP_HISTORY_MODE": "off"}.get(
            knob.name
        ),
    )
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_env_editing_collapses_duplicates(tmp_path):
    env = tmp_path / ".env"
    _write(env, "ZULIP_CHATMODE=oncall\nA=1\nZULIP_CHATMODE=onmessage\n")
    cli.run_config_wizard(
        env_path=env,
        prompter=lambda knob, current, source: {"ZULIP_CHATMODE": "onchar"}.get(
            knob.name
        ),
    )
    text = env.read_text(encoding="utf-8")
    assert text.count("ZULIP_CHATMODE") == 1
    assert "ZULIP_CHATMODE=onchar" in text
    assert "A=1" in text


# ------------------------------------------------------------- the command


def test_main_renders_the_configuration(capsys, recommended):
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "Zulip effective configuration" in out
    assert "profile: recommended" in out
    assert "ZULIP_DM_POLICY" in out


def test_main_advanced_lists_the_plumbing_knobs(capsys):
    cli.main(["config", "--advanced"])
    out = capsys.readouterr().out
    assert "ZULIP_QUEUE_CAP" in out


def test_main_defaults_to_config_with_no_subcommand(capsys):
    assert cli.main([]) == 0
    assert "Zulip effective configuration" in capsys.readouterr().out


def test_main_wizard_reports_the_edit(capsys, tmp_path, recommended):
    env = tmp_path / ".env"
    import builtins

    # Answers are consumed in CURATED_KNOBS order, and ZULIP_CHATMODE is first,
    # so pad up to the knob under test and keep the rest.
    index = [k.name for k in cli.CURATED_KNOBS].index("ZULIP_HISTORY_MODE")
    answers = iter([""] * index + ["always"] + [""] * (len(cli.CURATED_KNOBS) - index))
    original = builtins.input
    builtins.input = lambda *a, **k: next(answers)
    try:
        assert cli.run_config_command(wizard=True, env_path=env) == 0
    finally:
        builtins.input = original
    out = capsys.readouterr().out
    assert "ZULIP_HISTORY_MODE=always" in out
    assert "ZULIP_HISTORY_MODE=always" in env.read_text(encoding="utf-8")


# ------------------------------------------------------------- the shim


def test_config_sh_ships_like_update_sh():
    """A documented path that does not exist is a bug (#204/#208).

    ``config.sh`` is the only entry point to this command, so it must be in the
    package, listed for the updater, and checksummed -- exactly the contract
    ``update.sh`` is held to.
    """
    package_dir = Path(__file__).resolve().parent.parent / "zulip"
    script = package_dir / "config.sh"
    assert script.is_file(), "config.sh must live beside update.sh in the package"
    assert script.read_text(encoding="utf-8").splitlines()[0].startswith("#!")

    from zulip.version import PLUGIN_FILES

    assert "config.sh" in PLUGIN_FILES, "an unlisted file is deleted by the updater"

    checksums = (package_dir.parent / "checksums.txt").read_text(encoding="utf-8")
    assert "config.sh" in checksums, "an unchecksummed file is installed unverified"


def test_config_sh_runs_cli_as_a_module():
    """``python3 cli.py`` cannot work (relative imports), and neither can a bare
    ``python3`` (importing the package imports the Hermes host)."""
    script = (
        Path(__file__).resolve().parent.parent / "zulip" / "config.sh"
    ).read_text(encoding="utf-8")
    assert "-m zulip.cli config" in script
    assert "ZULIP_PYTHON" in script
    assert "cannot import the plugin" in script


def test_readme_documents_the_shim_next_to_update_sh():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
        encoding="utf-8"
    )
    assert "plugins/zulip/config.sh" in readme
    assert "plugins/zulip/update.sh" in readme
