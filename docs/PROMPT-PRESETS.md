# Prompt presets

How much guidance the plugin adds to a turn, and the part of it that no preset
can remove.

This is the prompt half of the work in
[docs/RECOMMENDED-PROFILE.md](RECOMMENDED-PROFILE.md) (epic #211). The profile
decides **what the adapter does**; a prompt preset decides **what the model is
told**, and the two never overlap.

---

## The two categories, and why they are split in code

One block of instructions used to be sent with every turn, mixing two things
that have opposite requirements:

| Category | Contents | May an operator trim it? |
|----------|----------|--------------------------|
| **Product behaviour** | identity · how a reply is produced · how a message reaches you · how topics work | **No.** Remove any of it and replies stop working correctly |
| **Domain guidance** | domain · repositories and pull requests · tooling · formatting | Yes — it earns its place for a coding bot and is noise for a greeter, a summariser or a triage bot |

The concern is not length. A prompt that has lost the addressing rule produces a
bot that answers the wrong message; one that has lost the threading rule answers
in the wrong topic. On a cheap model, a long irrelevant block also costs the
attention that the actual question needs.

So the split is structural. In `zulip/prompt.py`:

- **`CORE`** is a fixed tuple. It is *not* a section, it is not addressable by a
  preset, and there is no code path by which a preset removes a rule from it.
- **`SECTIONS`** is the addressable set, keyed by name.
- **`PRESETS`** maps a preset name to a list of **section names only**.

Adding a core rule is a code change. Dropping a section is a setting.

### What the core contains

The core is exactly these four rules, in this order — the names are the headings
`zulip/prompt.py` renders:

| Core rule | What it fixes in place |
|-----------|------------------------|
| **Identity** | that the bot is a Zulip bot, that its mention handle is its email prefix, and that a mention is stripped before the text reaches it |
| **How a reply is produced** | that the answer is the message text, that the gateway owns chunking / status reactions / typing / threading, and that the bot does not narrate that machinery |
| **How a message reaches you** | why this turn exists (mention, trigger prefix, or answer-everything), and what the observed-stream-traffic marker means |
| **Threading and topics** | that a topic is the unit of conversation, that replies stay in the topic they were addressed in, and that the gateway applies a topic directive |

---

## The presets

| `ZULIP_PROMPT_PRESET` | Renders | Use it for |
|-----------------------|---------|------------|
| `off` *(default)* | nothing at all | **Every install today.** The turn is byte-identical to what it was before this setting existed |
| `full` | the core + all four sections | a coding or ops bot that should carry the repo/PR conventions |
| `lean` | the core + `tooling` + `formatting` | a chat-shaped bot: a greeter, a summariser, a research reader, a triage bot |

### What `lean` drops, exactly

`lean` removes two sections and nothing else:

- **`domain`** — "this install is used for software work; prefer reading the code
  over guessing; match existing conventions; separate what you verified from what
  you assumed."
- **`repos`** — how to phrase a review around a defect class, and that GitHub
  links are validated before sending.

It keeps **`tooling`** (a bot that can act should not pretend it cannot) and
**`formatting`** (a wall of text reads badly in a room).

That is the whole difference. The core is identical in `full` and `lean`, and
the test suite renders both presets and asserts the core verbatim, so a preset
that silently lost a core rule fails CI rather than shipping.

### What a preset never changes

`off`, `full` and `lean` produce identical behaviour for: delivery, addressing,
mentions, threading, topics, chunking, status reactions, typing indicators, exec
approvals, policy and rate limiting. Those live in the adapter. A preset changes
which guidance is sent, never what the adapter does.

---

## Precedence and resolution

```
explicit env value  >  ZULIP_PROFILE=recommended  >  built-in default (off)
```

Resolved through the same gate as every other preset knob
(`zulip/runtime_scope.py::effective_value`), **at call time**, so a patched
module or a changed environment takes effect without re-importing. An empty or
blank value counts as unset, exactly as everywhere else in this plugin.

**An unknown value warns once and falls back to `full`, not to `lean`.** Falling
back to the trimmed set would withhold guidance an operator never asked to drop;
falling back to the complete set can only cost context, which is recoverable.
The warning is once per process so a typo cannot flood the log on every turn.

```bash
# Effective value and where it came from (env / profile / default)
bash ~/.hermes/plugins/zulip/config.sh | grep PROMPT_PRESET
```

---

## Where the text goes, and prefix caching

The rendered prompt is prepended to `channel_context` — the one per-turn text
channel this adapter owns that survives the host's native admission. The host
prepends that field to the turn, so the turn reads:

```
<prompt: core + selected sections>      <- static, byte-stable
<per-turn context, e.g. the soft-gate marker>   <- varies
[New message]
<the message, with any quoted history above it>
```

The static text comes **first** and contains no timestamp, message id or
per-turn count, so two turns with different context share an identical prefix
and a provider that caches prompt prefixes can cache it. This is enforced by
`tests/test_prompt.py::test_the_prefix_is_byte_stable_across_different_dynamic_context`.

**Honest caveat:** this lands in the *user* turn rather than the system prompt.
The gateway's system prompt is assembled from the session context, the YAML
`channel_prompts` and per-channel overrides, none of which a plugin can set. If
the guidance needs to be a true system prompt, that is a host capability
question, not something this plugin can decide.

---

## Verifying it yourself

```bash
# 1. Nothing changes until you ask for it.
unset ZULIP_PROMPT_PRESET           # turn byte-identical to before this existed

# 2. Turn the lean preset on and watch the prompt block shrink.
export ZULIP_PROMPT_PRESET=lean
#    With ZULIP_ACTIVITY_TRACE=1 the status board shows the context size
#    (#225), so the drop is visible in the room rather than inferred.

# 3. See exactly what is rendered, without a gateway.
python3 -c "import sys; sys.path.insert(0,'.'); \
from zulip import prompt; print(prompt.render_static('lean'))"
```

The tests that hold this document true:

| Test | Guards |
|------|--------|
| `test_every_preset_renders_the_core_verbatim` | a preset cannot drop a core rule |
| `test_the_core_covers_exactly_the_four_categories_the_issue_names` | the core cannot quietly grow into a domain dump |
| `test_lean_is_a_strict_subset_of_full` | `lean` actually drops something |
| `test_lean_drops_the_engineering_guidance` | the drop is the documented one |
| `test_off_renders_nothing_at_all` | the default is a no-op |
| `test_the_prefix_is_byte_stable_across_different_dynamic_context` | cacheable prefix |
| `test_an_unknown_preset_falls_back_to_full_not_lean` | the fallback direction |
| `test_the_prompt_precedes_the_turn_context` | static text before per-turn text |
| `test_a_broken_preset_never_drops_the_turn` | a broken preset cannot lose a message |
