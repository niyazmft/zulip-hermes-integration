"""The prompt surface: a non-removable core, and presets that select sections (#223).

The property this file exists for is short and absolute: **a preset may drop
guidance, and must never drop a rule that makes replies work.** Everything else
here protects the two things that follow from it — the core renders as a
byte-stable prefix, and an install that never sets the marker sends exactly what
it sent before this module existed.
"""

from __future__ import annotations

import pytest

from zulip import prompt as prompt_surface


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with no preset selected and the warn-cache empty."""
    monkeypatch.delenv(prompt_surface.PRESET_ENV, raising=False)
    prompt_surface._warned.clear()
    yield
    prompt_surface._warned.clear()


def _all_presets() -> list[str]:
    return [prompt_surface.PRESET_LEAN, prompt_surface.PRESET_FULL]


# ------------------------------------------------------------------ the core


@pytest.mark.parametrize("preset", _all_presets())
def test_every_preset_renders_the_core_verbatim(preset):
    """The acceptance test: removing a section cannot remove a core rule."""
    rendered = prompt_surface.render_static(preset)
    for heading, body in prompt_surface.CORE:
        assert f"## {heading}\n{body}" in rendered


def test_the_core_covers_exactly_the_four_categories_the_issue_names():
    headings = [heading for heading, _body in prompt_surface.CORE]
    assert headings == [
        "Identity",
        "How a reply is produced",
        "How a message reaches you",
        "Threading and topics",
    ]


def test_no_preset_can_address_the_core():
    """The core is not a section, so no preset value can select or drop it."""
    for preset in prompt_surface.PRESETS.values():
        assert not set(preset) & {heading for heading, _ in prompt_surface.CORE}
    assert set(prompt_surface.PRESETS[prompt_surface.PRESET_FULL]) <= set(
        prompt_surface.SECTIONS
    )


def test_an_unknown_preset_cannot_shrink_the_core():
    rendered = prompt_surface.render_static("experimental")
    for heading, body in prompt_surface.CORE:
        assert f"## {heading}\n{body}" in rendered


# --------------------------------------------------------------- the presets


def test_lean_is_a_strict_subset_of_full():
    full = set(prompt_surface.selected_sections(prompt_surface.PRESET_FULL))
    lean = set(prompt_surface.selected_sections(prompt_surface.PRESET_LEAN))
    assert lean < full, "the lean preset must drop something, or it is not lean"


def test_lean_drops_the_engineering_guidance():
    lean = prompt_surface.render_static(prompt_surface.PRESET_LEAN)
    full = prompt_surface.render_static(prompt_surface.PRESET_FULL)
    for name in ("domain", "repos"):
        assert prompt_surface.SECTIONS[name][0] not in lean
        assert prompt_surface.SECTIONS[name][0] in full


def test_off_renders_nothing_at_all():
    """An install that never sets the marker is byte-identical to before."""
    assert prompt_surface.render_static(prompt_surface.PRESET_OFF) == ""
    assert prompt_surface.render(prompt_surface.PRESET_OFF, "dynamic") == "dynamic"


def test_sections_render_in_a_fixed_order():
    full = prompt_surface.selected_sections(prompt_surface.PRESET_FULL)
    assert full == prompt_surface.SECTION_ORDER
    lean = prompt_surface.selected_sections(prompt_surface.PRESET_LEAN)
    assert list(lean) == [n for n in prompt_surface.SECTION_ORDER if n in lean]


# ------------------------------------------------------------ byte stability


def test_the_prefix_is_byte_stable_across_different_dynamic_context():
    """Two turns with different context must share an identical prefix."""
    first = prompt_surface.render(prompt_surface.PRESET_FULL, "history: 3 messages")
    second = prompt_surface.render(
        prompt_surface.PRESET_FULL,
        "observed: something else entirely\nand a second line",
    )
    prefix = prompt_surface.render_static(prompt_surface.PRESET_FULL)
    assert first.startswith(prefix)
    assert second.startswith(prefix)
    assert first[: len(prefix)] == second[: len(prefix)]


def test_dynamic_content_never_lands_inside_the_prefix():
    dynamic = "[Topic history - recent messages quoted for context]\n[Alice] hi"
    rendered = prompt_surface.render(prompt_surface.PRESET_FULL, dynamic)
    prefix = prompt_surface.render_static(prompt_surface.PRESET_FULL)
    assert rendered == f"{prefix}\n\n{dynamic}"


def test_the_static_text_carries_no_per_turn_fact():
    """Nothing in the prefix may change between turns, so nothing may vary.

    The check is on *facts* that vary (a measurement line, a message id, a
    timestamp), not on vocabulary: "each turn is one message" is a rule, and a
    rule is exactly what a prefix is allowed to contain.
    """
    prefix = prompt_surface.render_static(prompt_surface.PRESET_FULL)
    for marker in ("context:", " msgs", " tok", "message_id", "2026-", "[New message]"):
        assert marker not in prefix
    assert prefix == prompt_surface.render_static(prompt_surface.PRESET_FULL)


# -------------------------------------------------------------- resolution


def test_resolution_is_at_call_time(monkeypatch):
    assert prompt_surface.resolve_preset() == prompt_surface.PRESET_OFF
    monkeypatch.setenv(prompt_surface.PRESET_ENV, "lean")
    assert prompt_surface.resolve_preset() == prompt_surface.PRESET_LEAN
    assert prompt_surface.render_static().count("## ") == len(prompt_surface.CORE) + len(
        prompt_surface.PRESETS[prompt_surface.PRESET_LEAN]
    )


def test_a_blank_value_means_off(monkeypatch):
    monkeypatch.setenv(prompt_surface.PRESET_ENV, "   ")
    assert prompt_surface.resolve_preset() == prompt_surface.PRESET_OFF


def test_an_unknown_preset_falls_back_to_full_not_lean(monkeypatch, caplog):
    """Falling back to lean would withhold guidance nobody chose to drop."""
    monkeypatch.setenv(prompt_surface.PRESET_ENV, "slim")
    with caplog.at_level("WARNING"):
        assert prompt_surface.resolve_preset() == prompt_surface.PRESET_FULL
    assert "not a known preset" in caplog.text


def test_an_unknown_preset_warns_once_not_per_turn(monkeypatch, caplog):
    monkeypatch.setenv(prompt_surface.PRESET_ENV, "slim")
    with caplog.at_level("WARNING"):
        for _ in range(5):
            prompt_surface.resolve_preset()
    assert caplog.text.count("not a known preset") == 1


# --------------------------------------------------------------- the wiring


def test_off_leaves_the_turn_context_untouched(monkeypatch):
    from zulip import inbound

    monkeypatch.delenv(prompt_surface.PRESET_ENV, raising=False)
    assert inbound._with_prompt_prefix("") == ""
    marker = inbound.OBSERVED_STREAM_CONTEXT
    assert inbound._with_prompt_prefix(marker) == marker


def test_the_prompt_precedes_the_turn_context(monkeypatch):
    """The cacheable prefix must come first, the per-turn marker after it."""
    from zulip import inbound

    monkeypatch.setenv(prompt_surface.PRESET_ENV, "lean")
    composed = inbound._with_prompt_prefix(inbound.OBSERVED_STREAM_CONTEXT)
    prefix = prompt_surface.render_static(prompt_surface.PRESET_LEAN)
    assert composed == f"{prefix}\n\n{inbound.OBSERVED_STREAM_CONTEXT}"


def test_a_broken_preset_never_drops_the_turn(monkeypatch):
    from zulip import inbound

    def _boom():
        raise RuntimeError("preset exploded")

    monkeypatch.setattr(prompt_surface, "render_static", _boom)
    marker = inbound.OBSERVED_STREAM_CONTEXT
    assert inbound._with_prompt_prefix(marker) == marker
