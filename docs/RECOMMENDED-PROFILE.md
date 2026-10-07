# The Recommended Profile

`ZULIP_PROFILE=recommended` is a *named default posture*, not a second
configuration file. It supplies values for knobs the operator did not set, so a
fresh install with only the three credentials behaves like a complete
shared-room teammate without editing anything. Every knob stays settable; only
*which* decisions are asked of you changes.

This document is the checkable spec: what the profile decides, what it must
never change, and how to tell whether it is working.

---

## Target user and posture

| | |
|---|---|
| **Target user** | A solo self-hoster on a **shared work Zulip**, with other humans present |
| **Posture** | A shared teammate in streams; **DMs belong to the owner** |

The knobs stay available because this is a product decision, not a capability
limit. What the profile removes is the requirement to answer ~50 questions
before the bot is useful.

---

## Locked design decisions

| Decision | Value | Where it lives |
|---|---|---|
| **Owner identity** | The Zulip **bot owner**, fetched from the API — zero typing. `ZULIP_OWNER_EMAIL` is the explicit override. | `zulip/policy.py`, `ZulipAdapter.seed_bot_owner_dm_allowlist` |
| **Setup** | Three credentials, then **one** question: *"Use the recommended setup?"* (default yes) | `zulip/cli.py::interactive_setup` |
| **Later access** | `zulip config` — effective-config view plus a curated wizard | `zulip/cli.py::main`, `zulip/config.sh` |
| **Trigger scope** | Every stream the bot is subscribed to, **mention-gated** | `ZULIP_CHATMODE=oncall` |
| **Memory scope** | **Conversation-scoped** — a topic is remembered only once the bot was addressed in it | `zulip/history.py::AddressedTopicTracker` |
| **Reaction triggers** | `+1` proceed · `repeat` / `arrows_counterclockwise` different approach · `test_tube` tests · `question` explain. Fireable by anyone who can already mention the bot. | `zulip/runtime_scope.py::RECOMMENDED_REACTION_TRIGGERS` |
| **Mechanism** | One gate, resolved as **explicit env > preset > built-in default** | `zulip/runtime_scope.py::resolve_setting` |

**On by default:** activity trace, history context (`on-demand`), per-session
queue, stream observation, reaction triggers, per-topic sessions.

**Off by default:** sticky engagement, actionable refs. The preset supplies no
value for these, so their built-in defaults stand.

**Fail closed:** an unanswered exec approval is a refusal, and under the preset
the bot states that refusal in the topic and records it. This is a *statement*
about silence, not a second decision path: the gateway owns the approval
`timeout` and refuses an unanswered request on every host this plugin supports,
so no configuration can make silence run a command (#222).

**Owner-only approvals:** a prompt is posted into a topic, so "anyone who can
read it" is a privilege escalation through a side channel — a coworker approving
the owner's bot running a command on the owner's host. The preset restricts the
decision to the bot owner #214 resolves, and refuses rather than falling back to
open when no owner can be resolved (#228).

---

## What the preset actually sets

| Knob | Under `recommended` | Built-in default | Note |
|---|---|---|---|
| `ZULIP_CHATMODE` | `oncall` | `onmessage` | **This** is what makes the profile mention-gated — see below |
| `ZULIP_REQUIRE_MENTION` | `true` | `true` | Inert in every mode; kept as a statement of intent |
| `ZULIP_GROUP_POLICY` | `open` | `open` | Pinned explicitly so a later default change cannot move the profile |
| `ZULIP_DM_POLICY` | `allowlist` | `open` | Combined with owner seeding, this is "DMs are the owner's" |
| `ZULIP_ACTIVITY_TRACE` | `true` | `false` | |
| `ZULIP_HISTORY_MODE` | `on-demand` | `off` | Harvests only for history-shaped questions |
| `ZULIP_SESSION_QUEUE` | `true` | `false` | |
| `ZULIP_OBSERVE_GROUP` | `true` | `false` | Needs a drop path to do anything — see the exclusion below |
| `ZULIP_TOPIC_SESSIONS` | `true` | `false` | |
| `ZULIP_SOFT_GATE` | `false` | `false` | |
| `ZULIP_APPROVAL_ON_TIMEOUT` | `deny` | `allow` | Silence is a refusal, and the bot says so and records it. The gateway owns the timeout and refuses either way (#222) |
| `ZULIP_APPROVAL_AUTHORITY` | `owner` | `anyone` | Only the bot owner may decide an exec approval — the same identity #214 resolves for the DM allowlist (#228) |
| `ZULIP_REACTION_TRIGGERS` | the five above | none | Off by default for legacy installs |

### Why the preset moves `ZULIP_CHATMODE` when the epic's table named `ZULIP_REQUIRE_MENTION`

`ZULIP_REQUIRE_MENTION` is **inert in every mode**. Each trigger mode already
implies its own condition (`oncall` → mentioned, `onchar` → prefix or
mentioned), so the additional gate in `zulip/inbound.py` can only lower a
decision that is already `False`. `zulip/settings.py` already said so
("inert in every mode").

Mention-gating therefore has to come from the chatmode, whose built-in default
`onmessage` answers **every** message. Left at `onmessage`, the profile would:

* reply to everything in every subscribed stream, contradicting acceptance
  criteria 1 and 7 below; and
* make `ZULIP_OBSERVE_GROUP=on` a **no-op**, because observation happens only on
  the drop path and `onmessage` drops nothing.

`ZULIP_REQUIRE_MENTION` is still set, as the statement of intent it always was.
Reversing this is a one-line change to `RECOMMENDED_PRESET`.

---

## Precedence and migration safety

```
explicit env value  >  preset (only when ZULIP_PROFILE=recommended)  >  built-in default
```

`zulip/runtime_scope.py::resolve_setting` is the single place this is applied,
and it returns the value **and its source** (`env` / `profile` / `default`) —
which is what `zulip config` prints.

**A variable that is present but empty counts as not explicitly set**, matching
how the existing resolvers already treat it.

### The contract

An install **without** `ZULIP_PROFILE` behaves **byte-for-byte** as it did
before the flag existed. Issue #153's rule — unset means the pre-flag
behaviour — is preserved. The preset supplies a value only when the marker is
present *and* the knob is not explicitly set, so no existing user's
configuration changes on upgrade.

This is not a comment, it is a test. `tests/test_profile_gate.py` asserts it
twice: every preset knob resolves to source `default` with the marker absent,
**and** the real legacy resolvers (`settings`, `PolicyEngine`, `TraceConfig`,
`ReactionTriggerConfig`) are called and compared against the values in the
"Built-in default" column above. The second half stays load-bearing: a wiring
that consults the preset without the marker turns it red.

---

## The soft-gate / observe exclusion

`ZULIP_SOFT_GATE` and `ZULIP_OBSERVE_GROUP` are **mutually exclusive**. The soft
gate dispatches every monitored stream message, so nothing reaches the drop path
where observation happens and observation silently becomes a no-op.

The preset therefore picks **soft gate off, observe on**. Both the adapter's
startup warning and `zulip config` say so in the *same words* — one constant,
`zulip/settings.py::SOFT_GATE_OBSERVE_CONFLICT`, because prose duplicated between
a log line and a CLI drifts, and the one that drifts is the one nobody reads.

---

## Observation: two behaviours, stated plainly

`ZULIP_OBSERVE_GROUP` buffers non-addressed stream messages and quotes them into
the prompt the next time the bot is addressed in that topic. Where it buffers
differs by profile:

| | Without `ZULIP_PROFILE` | With `ZULIP_PROFILE=recommended` |
|---|---|---|
| **Scope** | **Room-scoped** — every monitored topic is buffered, addressed or not | **Conversation-scoped** — only topics the bot has been addressed in |
| **Behaviour** | Unchanged from before the profile existed | A topic the bot was never addressed in produces no memory |

Both are deliberate. Existing opt-in observe users chose the room-scoped
behaviour, and narrowing it on upgrade would be a second silent change — exactly
what the migration contract forbids. `zulip/history.py::AddressedTopicTracker`
records engagement, bounded and least-recently-used like the buffer itself, and
keyed identically so the two cannot disagree about which conversation they mean.

### The preset interaction

Under the recommended profile `ZULIP_OBSERVE_GROUP` is **already on**, supplied by
the preset — the gate engages with the operator setting nothing. Turning
observation off takes an explicit `ZULIP_OBSERVE_GROUP=false`, because an explicit
value beats the preset.

---

## Owner seeding, and what happens when it fails

Because the preset sets `ZULIP_DM_POLICY=allowlist` but a fresh install has no
address to allow-list, the owner is read from Zulip at connect — reusing the
`GET /users/me` payload the connection already fetches to validate credentials,
so discovery costs **no extra round-trip**. `bot_owner_id` resolves to an address
through `zulip_client.user_lookup_call` (the SDK exposes no single consistent
single-user method — issue #196); a payload that inlines the address skips the
lookup.

The lookup runs only when **all three** hold: the profile is `recommended`, the
policy consults the allowlist, and no owner was named. Auto-detecting for every
`allowlist` install would widen an existing user's allowlist on upgrade — a
silent authorization change.

Failure is **loud and non-fatal**: no owner in the payload, no supported lookup
method, an API error, or a payload that names nobody each log one actionable
warning naming `ZULIP_OWNER_EMAIL`, then return. Streams still work. An address
that is not address-shaped is refused rather than seeded, because an allowlist
entry that can never match a sender looks like it works.

---

## Why the code is shaped this way

Two placements are forced, and are not accidents:

* **The preset table and resolver live in `zulip/runtime_scope.py` (L0).** Adding
  a new leaf module would mean editing `LAYERS` in `tests/test_layering.py`, which
  the epic deliberately avoids.
* **The reaction-trigger default is resolved in the adapter (L6), not in
  `reaction_triggers` (L0).** `reaction_triggers` cannot import `runtime_scope`,
  because `tests/test_layering.py` forbids same-level imports. `from_env()`
  remains as the env-only legacy path.

---

## The tier model

| Tier | Size | Surface |
|---|---|---|
| **Curated** | 11 | The decisions that change what the bot *is*: trigger mode, group policy, DM policy, owner, trace, history, observation, session queue, per-topic sessions, reaction triggers, rate limit |
| **Advanced** | every declared knob | Everything the plugin reads — `zulip config --advanced` prints the exact count, which grows as knobs are added |
| **Derived** | the rest | Plumbing with a defensible default; never asked about |

`zulip config` prints the **effective** value and the **source** of every knob, so
"what did it decide for me?" is answerable without reading the code. The wizard
writes **only deviations**: answering with the value the profile already supplies
*removes* the entry rather than restating it, so `.env` stays a short list of
where this install differs instead of a copy of the profile.

The curated tier deliberately omits `ZULIP_REQUIRE_MENTION` — offering a knob
that does nothing is worse than omitting it. `ZULIP_CHATMODE`, which actually
gates, is offered instead.

---

## Acceptance criteria

A fresh install with only `ZULIP_SITE` / `ZULIP_EMAIL` / `ZULIP_API_KEY` and
**zero file edits** must:

1. Answer @mentions in every subscribed stream, threaded to the topic.
2. DM correctly for the bot owner only; a coworker's DM is refused.
3. Show a live activity-trace board on a long run.
4. Continue a multi-message exchange in a topic — and remember **only** topics it
   was addressed in.
5. Honour `👍` / `🔁` / `🧪` / `❓` on its own messages.
6. Queue a second sender's message behind a running turn rather than redirecting
   it.
7. Never leak one topic's context into another, and never reply where it was not
   called.

Plus: `zulip config` prints the effective configuration with the **source** of
each value (`env` / `profile` / `default`), and an install with no
`ZULIP_PROFILE` reports no behaviour change.

---

## Verifying it yourself

```bash
bash ~/.hermes/plugins/zulip/config.sh             # effective view, with sources
bash ~/.hermes/plugins/zulip/config.sh --advanced  # every knob the plugin reads
bash ~/.hermes/plugins/zulip/config.sh --wizard    # change them, deviations only
```

An install with no profile prints `profile: <none>` and reports `default` for
every knob — which is the migration contract, visible.

Code-checkable claims live in `tests/`: `test_profile_gate.py` (the gate and the
no-marker contract), `test_profile_resolvers.py` (every knob under the profile,
and every override winning), `test_owner_allowlist.py` (owner seeding and its
failure paths), `test_setup_profile.py` (the one-question setup),
`test_config_command.py` (provenance, the wizard, the shim), and
`test_conversation_scoped_observe.py` (the three observation criteria).
