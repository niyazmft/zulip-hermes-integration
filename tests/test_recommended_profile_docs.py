"""Drift guards for ``docs/RECOMMENDED-PROFILE.md`` (epic #211, child #218).

The profile changes defaults that other files tell operators to set by hand, so
the spec is only worth having if it stays true. The strongest guard here is the
**knob inventory cross-check**: every knob in ``RECOMMENDED_PRESET`` must be named
in the document, so adding a preset entry without documenting it fails CI rather
than quietly shipping an undocumented default.

The acceptance criteria are asserted by their distinctive wording, because they
are the epic's contract, not a summary of it.
"""

from __future__ import annotations

from pathlib import Path

from zulip import runtime_scope

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "docs" / "RECOMMENDED-PROFILE.md"


def _spec() -> str:
    assert SPEC.is_file(), "docs/RECOMMENDED-PROFILE.md must exist"
    return SPEC.read_text(encoding="utf-8")


def test_spec_exists_and_is_linked_from_the_readme():
    assert SPEC.is_file()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/RECOMMENDED-PROFILE.md" in readme, (
        "the README must link the spec, or nobody will find it"
    )


def test_every_preset_knob_is_documented():
    """Add a preset entry without documenting it and this fails."""
    text = _spec()
    missing = sorted(
        name for name in runtime_scope.RECOMMENDED_PRESET if name not in text
    )
    assert not missing, (
        "these preset knobs are not documented in docs/RECOMMENDED-PROFILE.md: "
        + ", ".join(missing)
    )


def test_the_precedence_rule_is_stated():
    text = _spec()
    assert "explicit env value" in text
    assert "built-in default" in text
    assert runtime_scope.PROFILE_RECOMMENDED in text
    for source in (
        runtime_scope.SOURCE_ENV,
        runtime_scope.SOURCE_PROFILE,
        runtime_scope.SOURCE_DEFAULT,
    ):
        assert f"`{source}`" in text, f"the `{source}` provenance is not documented"


def test_the_migration_contract_is_stated():
    text = _spec()
    assert "byte-for-byte" in text
    assert "issue #153" in text.lower() or "#153" in text
    # ...and it points at the test that actually enforces it.
    assert "test_profile_gate.py" in text


def test_both_observation_behaviours_are_stated_plainly():
    """#217 introduced a second behaviour; the spec must admit both (#217, #218)."""
    text = _spec()
    assert "Room-scoped" in text
    assert "Conversation-scoped" in text
    # The scoping is a profile behaviour, and its tracker is named.
    assert "AddressedTopicTracker" in text


def test_the_soft_gate_exclusion_is_documented_with_its_reason():
    text = _spec()
    assert "mutually exclusive" in text
    assert "no-op" in text
    assert "SOFT_GATE_OBSERVE_CONFLICT" in text, (
        "the spec should name the shared constant, so the CLI and the log stay one string"
    )


def test_the_layering_constraint_is_documented():
    """The placements are forced by tests/test_layering.py, not preferences."""
    text = _spec()
    assert "reaction_triggers" in text
    assert "runtime_scope" in text
    assert "same-level imports" in text or "LAYERS" in text


def test_the_tier_model_is_documented():
    text = _spec()
    assert "Curated" in text
    assert "Advanced" in text
    assert "Derived" in text


def test_the_seven_acceptance_criteria_are_verbatim():
    text = _spec()
    for criterion in (
        "Answer @mentions in every subscribed stream, threaded to the topic.",
        "DM correctly for the bot owner only; a coworker's DM is refused.",
        "Show a live activity-trace board on a long run.",
        "remember **only** topics it",
        "Honour `👍` / `🔁` / `🧪` / `❓` on its own messages.",
        "Queue a second sender's message behind a running turn rather than redirecting",
        "Never leak one topic's context into another, and never reply where it was not",
    ):
        assert criterion in text, f"acceptance criterion missing or reworded: {criterion!r}"


def test_agents_md_warns_that_a_preset_is_not_an_admin_setting():
    """An agent must not tell a user to \"ask the admin\" about a profile default."""
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "ZULIP_PROFILE=recommended" in agents
    assert "Recommended profile" in agents
    assert "docs/RECOMMENDED-PROFILE.md" in agents


def test_security_md_states_the_defaults_the_profile_moves():
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "ZULIP_PROFILE=recommended" in security
    # The three moved defaults, and the one that deliberately does not move.
    assert "seed_dm_allowlist" in security
    assert "ObservedContextBuffer" in security
    assert "ZULIP_ACTIVITY_TRACE` becomes on" in security
    assert "stream posture does not move" in security.lower() or (
        "The stream posture does not move" in security
    )
