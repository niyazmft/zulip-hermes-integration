"""Drift guards for ``docs/PROMPT-PRESETS.md`` (issue #223).

The document exists because an operator has to be able to answer two questions
without reading code: *what does the lean preset actually drop?* and *is there
anything I can trim that will break replies?* A doc that drifts from
``zulip/prompt.py`` answers both wrongly, so the strongest guard here is the
inventory cross-check — every preset name and every section name must appear in
the document, and the one thing no preset may drop must be stated.
"""

from __future__ import annotations

from pathlib import Path

from zulip import prompt as prompt_surface

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PROMPT-PRESETS.md"


def _doc() -> str:
    assert DOC.is_file(), "docs/PROMPT-PRESETS.md must exist"
    return DOC.read_text(encoding="utf-8")


def test_the_doc_exists_and_is_linked_from_the_readme():
    assert DOC.is_file()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/PROMPT-PRESETS.md" in readme, (
        "the README must link the doc, or nobody will find it"
    )


def test_the_profile_spec_points_at_it():
    """The two surfaces are adjacent, so the spec must not look complete alone."""
    spec = (REPO_ROOT / "docs" / "RECOMMENDED-PROFILE.md").read_text(encoding="utf-8")
    assert "docs/PROMPT-PRESETS.md" in spec or "PROMPT-PRESETS.md" in spec


def test_every_preset_name_is_documented():
    text = _doc()
    missing = sorted(
        name for name in (*prompt_surface.PRESETS, prompt_surface.PRESET_OFF)
        if f"`{name}`" not in text
    )
    assert not missing, "these presets are not documented: " + ", ".join(missing)


def test_every_section_is_documented():
    """Add a section without documenting it and this fails."""
    text = _doc()
    missing = sorted(name for name in prompt_surface.SECTIONS if f"`{name}`" not in text)
    assert not missing, (
        "these sections are not documented in docs/PROMPT-PRESETS.md: "
        + ", ".join(missing)
    )


def test_what_lean_drops_is_stated_plainly():
    """The acceptance criterion: say plainly what the lean preset drops."""
    text = _doc()
    dropped = set(prompt_surface.SECTIONS) - set(
        prompt_surface.PRESETS[prompt_surface.PRESET_LEAN]
    )
    assert dropped, "the lean preset must drop something, or there is nothing to state"
    for name in dropped:
        assert f"`{name}`" in text, f"the lean preset drops {name} but the doc omits it"
    assert "What `lean` drops" in text


def _flat() -> str:
    """The document with runs of whitespace collapsed.

    Markdown wraps at ~90 columns, so a phrase a test asserts on can be split
    across a newline by an editor with no semantic change. Assert on the text,
    not on where the wrapping happens to fall.
    """
    return " ".join(_doc().split())


def test_the_irremovable_core_is_stated():
    text = _flat()
    assert "no preset can remove" in text or "cannot be removed" in text
    for heading, _body in prompt_surface.CORE:
        assert heading in text, f"core rule {heading!r} is not named in the doc"


def test_the_fallback_direction_is_stated():
    """An unknown value must be documented as falling back to the *full* set."""
    assert "falls back to `full`" in _flat()


def test_the_precedence_rule_is_stated():
    assert "explicit env value" in _flat()


def test_the_non_effects_are_stated():
    """A preset must never be mistaken for a behaviour switch."""
    text = _doc()
    for behaviour in ("delivery", "addressing", "threading", "chunking", "approvals"):
        assert behaviour in text


def test_the_default_is_documented_as_a_no_op():
    text = _doc()
    assert "byte-identical" in text
