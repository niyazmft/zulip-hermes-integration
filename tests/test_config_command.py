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
import sys
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


def test_the_profile_itself_is_shown_in_the_curated_view(recommended):
    """"What did it decide for me?" is unanswerable without the posture.

    The profile was visible only under ``--advanced``, which is the tier an
    operator reaches for *after* the curated view has failed to answer. It is the
    one knob that decides every other row, so it belongs in the curated view.
    """
    rows = {row.name: row for row in cli.effective_config()}
    assert "ZULIP_PROFILE" in rows
    assert rows["ZULIP_PROFILE"].value == "recommended"
    assert rows["ZULIP_PROFILE"].source == runtime_scope.SOURCE_ENV


def test_an_unset_profile_is_shown_rather_than_omitted():
    """Absence is a decision too: it means the legacy defaults."""
    rows = {row.name: row for row in cli.effective_config()}
    assert rows["ZULIP_PROFILE"].source == runtime_scope.SOURCE_DEFAULT
    assert "<unset>" in cli.render_config()


def test_the_curated_view_never_lists_the_profile_twice(recommended):
    """It is added by the curated pass *and* declared in the manifest."""
    for advanced in (False, True):
        names = [row.name for row in cli.effective_config(advanced=advanced)]
        assert names.count("ZULIP_PROFILE") == 1


def test_the_wizard_does_not_ask_about_the_profile():
    """The recorded decision, so undoing it takes a deliberate edit.

    ``baseline_value`` measures every other curated knob *against* the profile, so
    it cannot also be a deviation from itself: answering one wizard question could
    silently rewrite the meaning of every answer collected in the same run. Making
    it settable needs a decision about what the wizard writes when you pick it
    (#299).
    """
    assert "ZULIP_PROFILE" not in CURATED_NAMES
    assert "ZULIP_PROFILE" in {name for name, _ in cli.declared_settings()}


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


def test_the_dependency_free_reader_agrees_with_pyyaml():
    """Two readers, one manifest: they must produce the same list.

    The dependency-free reader is the degraded path, so it is only safe while it
    agrees with the real parser. This test is the tie; it runs wherever PyYAML is
    installed (CI, this machine) and is skipped where it is not, which is exactly
    the environment the fallback exists for.
    """
    yaml = pytest.importorskip("yaml")
    text = cli.manifest_path().read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    expected: list[tuple[str, str]] = []
    for section in ("requires_env", "optional_env"):
        for entry in data.get(section) or []:
            expected.append((entry["name"], str(entry.get("description", "")).strip()))

    assert cli._declared_settings_without_yaml(text) == expected


def test_declared_settings_works_when_pyyaml_is_absent(monkeypatch):
    """``config.sh --advanced`` must not need a package the host may not have.

    lab-hermes ships ``ruamel.yaml`` and no ``yaml`` at all, so the lazy import
    turned the documented command into a traceback. Installing PyYAML into the
    gateway's venv would fix that one machine and mutate the environment Hermes
    itself runs from -- the manifest reader above is the fix instead.
    """
    monkeypatch.setitem(sys.modules, "yaml", None)  # `import yaml` now raises ImportError

    rows = cli.declared_settings()
    names = [name for name, _ in rows]
    assert names, "the fallback must still list every declared knob"
    assert names[0] == "ZULIP_API_KEY", "manifest order must survive the fallback"
    assert "ZULIP_PROFILE" in names
    assert "ZULIP_QUEUE_MAX_ACTIVE_SECONDS" in names
    assert all(description for _, description in rows), (
        "a description lost by the fallback leaves --advanced looking blank"
    )


def test_the_advanced_view_still_works_without_pyyaml(monkeypatch):
    """The command a user actually runs, under the same degraded condition."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    rows = cli.effective_config(advanced=True)
    names = {row.name for row in rows}
    assert "ZULIP_PROFILE" in names
    assert len(names) == len(rows), "the advanced view must not list a knob twice"
    assert "--advanced" in cli.render_config(advanced=True) or len(rows) > 50


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
