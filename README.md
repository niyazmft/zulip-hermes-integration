# 📬 Zulip Plugin for Hermes

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![Tests](https://github.com/niyazmft/zulip-hermes-integration/actions/workflows/ci.yml/badge.svg?branch=main&label=tests)](https://github.com/niyazmft/zulip-hermes-integration/actions/workflows/ci.yml)
[![Hermes](https://img.shields.io/badge/Hermes-%3E%3D0.18.2-green)](https://hermes-agent.nousresearch.com)
[![Latest Release](https://img.shields.io/github/v/release/niyazmft/zulip-hermes-integration?label=release)](https://github.com/niyazmft/zulip-hermes-integration/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Hermes gateway adapter for Zulip streams and private messages, with topic threading, traffic policies, and observability.

> 🔗 **Part of a single Zulip adapter family for open-source AI agents.**
> This repo is the **Hermes (Nous Research)** adapter (Python). Its sibling,
> [`openclaw-zulip-bridge`](https://github.com/niyazmft/openclaw-zulip-bridge), does the
> same thing for the **OpenClaw** agent (TypeScript). Same thesis, two runtimes:
> bring a self-hosted AI agent into threaded, topic-first Zulip chat as a full teammate —
> sovereign, no chat-vendor lock-in. The pattern Slack and Block's Buzz are racing to
> productize, delivered **open source** and **self-hosted**.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Verification](#verification)
- [Features](#features)
- [Configuration](#configuration)
- [Slash Commands](#slash-commands)
- [Progressive Activity Trace](#progressive-activity-trace)
- [History-aware Context](#history-aware-context)
- [In-Channel Action Triggers](#in-channel-action-triggers)
- [Exec Approvals](#exec-approvals)
- [Sticky Engagement](#sticky-engagement)
- [Per-Session Queue](#per-session-queue)
- [Stream Watching](#stream-watching)
- [Actionable Refs](#actionable-refs)
- [Sending Files](#sending-files)
- [Environment Variable Reference](#environment-variable-reference)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Documentation](#documentation)

## Prerequisites

- **Hermes** `>= 0.18.2` (native exec-approval buttons need `>= 0.21.3`; below that, typing
  clears on the stream's last-seen topic — see [gateway compatibility](AGENTS.md#-gateway-compatibility))
- **Python** 3.10+
- **A Zulip bot** — see below

### Creating a Zulip Bot

1. In Zulip, go to **Settings → Bots → Add a new bot**
2. Choose **Generic bot**
3. Copy the **bot email** and **API key** — Quick Start step 3 needs both
4. **Subscribe the bot to the streams it should answer in** (Stream settings → Subscribers). A bot that is not subscribed never sees those messages.

## Quick Start

```bash
# 1. Install the Zulip SDK into the same Python env as Hermes (one-time)
pip install "zulip>=0.9.0"

# 2. Install the plugin
mkdir -p ~/.hermes/plugins
rm -rf ~/.hermes/plugins/zulip
git clone https://github.com/niyazmft/zulip-hermes-integration.git ~/.hermes/plugins/zulip
hermes plugins enable zulip

# 3. Add credentials to ~/.hermes/.env
#    ZULIP_API_KEY=your-bot-api-key
#    ZULIP_EMAIL=your-bot@your-org.zulipchat.com
#    ZULIP_SITE=https://your-org.zulipchat.com

# 4. Start the gateway
hermes gateway
```

Step 3 can be done for you: `hermes gateway setup` prompts for each declared setting and
writes them to `~/.hermes/.env`, so nothing needs editing by hand.

Then tell the gateway to run the platform, in `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    zulip:
      enabled: true
```

> ⚠️ Install the whole repository, not individual files — the plugin is 28 modules that
> import from each other, so copying only `adapter.py` fails to load.

### Recommended setup — the whole product, one question

The plugin reads 50+ knobs, but a fresh install needs none of them. Run setup,
answer **one** question, and stop:

```bash
hermes gateway setup   # site, bot email, API key, then "Use the recommended setup?" (yes)
```

Answering yes writes `ZULIP_PROFILE=recommended` and asks nothing else — no
prompt wall, and `.env` stays at three credentials plus that one line. That
marker turns on a complete shared-room teammate: mention-gated streams, DMs for
the bot's Zulip owner only, the activity trace, on-demand history, observation,
per-session queueing, per-topic sessions and the reaction triggers. Sticky
engagement and actionable refs stay off. Any `ZULIP_*` value you set later still
wins over it.

To see what it decided — every value with its source, `env` / `profile` /
`default` — or to change it:

```bash
bash ~/.hermes/plugins/zulip/config.sh             # the settings that matter
bash ~/.hermes/plugins/zulip/config.sh --advanced  # every knob the plugin reads
bash ~/.hermes/plugins/zulip/config.sh --wizard    # change them, deviations only
```

The [Environment Variable Reference](#environment-variable-reference) below is
the **advanced** surface: every knob, with examples.

**Unset `ZULIP_PROFILE` and none of this applies.** An install without the marker
behaves exactly as it did before the flag existed — that is a tested contract, not
a hope. The full spec is in
[docs/RECOMMENDED-PROFILE.md](docs/RECOMMENDED-PROFILE.md).

### Container / system-wide install

To install for every user on the host instead:

```bash
HERMES_PATH=$(python3 -c "import hermes_cli; print(hermes_cli.__path__[0])")
rm -rf "$HERMES_PATH/../plugins/platforms/zulip"
git clone https://github.com/niyazmft/zulip-hermes-integration.git \
  "$HERMES_PATH/../plugins/platforms/zulip"
```

### Updating

```bash
bash ~/.hermes/plugins/zulip/update.sh     # pulls latest, verifies checksums, restarts
```

On a remote host, wrap it: `ssh user@device "bash ~/.hermes/plugins/zulip/update.sh"`.
Or by hand:

```bash
cd ~/.hermes/plugins/zulip && git pull origin main && hermes gateway restart
```

### Inspecting and changing settings

With `ZULIP_PROFILE=recommended` you should rarely need to set anything, but
`zulip config` answers "what did it decide for me?" by printing every knob with
the **source** of its value — `env` (you set it), `profile` (supplied by the
preset) or `default` (the built-in):

```bash
bash ~/.hermes/plugins/zulip/config.sh             # the settings that matter
bash ~/.hermes/plugins/zulip/config.sh --advanced  # every knob the plugin reads
bash ~/.hermes/plugins/zulip/config.sh --wizard    # change them, interactively
```

`config.sh` needs the same Python that runs the gateway, because importing the
plugin imports the Hermes host. It finds that automatically from the `hermes`
launcher; set `ZULIP_PYTHON=/path/to/that/python` if it cannot.

The wizard writes **only deviations** to `.env`: answering with the value the
profile (or the built-in default) already supplies removes the entry instead of
restating it, so the file stays a short list of where this install differs
rather than a copy of the profile.

DM the bot, or @-mention it in a stream it is subscribed to.

## Verification

Send one message and confirm the bot answers:

```
# In a DM
hello

# In a stream (the bot must be subscribed to it)
@**hermes-bot** hello
```

A reply means the plugin loaded, authenticated and connected. If nothing arrives:

```bash
grep -i zulip ~/.hermes/logs/gateway.log | tail -20
```

You should see `zulip probe ok`, `zulip bot authenticated` and
`zulip connection established`. If you see `zulip drop` instead, the message was
filtered — see [Troubleshooting](#troubleshooting).

## Features

**Core** — work as soon as credentials are set:

| Feature | What it does | Use it when |
|---------|--------------|-------------|
| **Streams & Topics** | talk to the bot in public streams; replies stay threaded to the topic they came from | the work should be visible to the team |
| **DMs** | private messages, with per-user session isolation and policy controls | it's between you and the bot |
| **Slash Commands** | `/streams`, `/user`, `/pin`, `/unpin` are answered directly; `/help`, `/status`, `/model` pass through to Hermes | you want a quick answer without spending a model call |
| **Status Reactions** | 👀 while working → ✅ when done (⚠️ on error) | you want to know the turn is alive without reading it |
| **Typing Indicator** | Zulip shows the bot as typing while it works | you want to see it is alive without reading a reply |
| **File Attachments** | inbound CSVs, PDFs and JSON are downloaded and readable; outbound files are sent as uploads | you're handing the bot a file, or want one back |
| **Persistent Event Queue** | resumes from where it left off across restarts | you restart the gateway |
| **Durable Deduplication** | an on-disk store stops a replayed event being processed twice | a restart or reconnect might replay a message |
| **Bot Workspace** | sandboxed file storage under `{data_dir}/workspace/`, path traversal and symlinks refused | the bot generates a report you want back |
| **SSRF Protection** | rejects internal IPs, localhost and cloud-metadata endpoints | a user can hand the bot a URL |
| **Secret Guard** | blocks an outbound message containing a host credential value | you don't want a token pasted into a room |

**Opt-in** — each is one flag away, and documented in its own section:

| Feature | What it does | Turn it on when |
|---------|--------------|-----------------|
| [**Progressive Activity Trace**](#progressive-activity-trace) | one status message per run, edited in place as work proceeds | long runs look like silence |
| [**History-aware Context**](#history-aware-context) | quotes real earlier messages from this topic into the prompt | people ask "didn't we hit this before?" |
| [**In-Channel Action Triggers**](#in-channel-action-triggers) | a 👍 on the bot's own message dispatches an instruction as a turn | you'd rather react than type "go ahead" |
| [**Sticky Engagement**](#sticky-engagement) | follow-ups continue without re-mentioning the bot | a conversation takes more than one message |
| [**Per-Session Queue**](#per-session-queue) | a message arriving mid-run waits instead of steering the running turn | several people share one topic |
| [**Stream Watching**](#stream-watching) | watch a busy stream, or silently remember what it says | the bot should know the room without answering it |
| [**Actionable Refs**](#actionable-refs) | GitHub links the agent writes are validated before rendering | you want to trust the links in a reply |

**Admin & security**:

| Feature | What it does | Use it when |
|---------|--------------|-------------|
| **DM Policies** | `open`, `allowlist`, `pairing` or `disabled` | you need to control who can reach the bot privately |
| **Group Policy** | who may trigger the bot in streams (`open`, `allowlist`, `disabled`) | a stream is busier than the bot should answer |
| **Rate Limiting** | per-sender sliding window (default 60 msg/min) | one noisy sender could otherwise flood it |
| **Audit Logging** | JSON-line log recording policy blocks, reaction triggers and queue transitions | you need to prove what happened, after the fact |
| **Health Probe** | pre-flight server check with SSRF protection and `health_status` logging | you're diagnosing a connection problem |

## Configuration

Credentials are set in [Quick Start](#quick-start). This section covers how the bot decides
**when** to answer; every knob is also in the
[environment reference](#environment-variable-reference).

### Stream trigger modes

`ZULIP_CHATMODE` decides **when** the bot answers in a stream:

| Mode | The bot answers when… | Example |
|------|----------------------|-------------------|
| `onmessage` *(default)* | every message in a monitored stream | everyone in `#general` is talking to the bot |
| `oncall` | it is @-mentioned | `@**hermes-bot** what changed today?` |
| `onchar` | a prefix is typed (`ZULIP_ONCHAR_PREFIXES`, default `!,>`) | `> summarise this thread` |

`ZULIP_REQUIRE_MENTION=true` (default) additionally requires a mention in `onmessage` mode,
which makes `onmessage` behave like `oncall`. If you want a quiet stream, prefer `oncall`
and leave this alone.

A mention is detected from Zulip's own `mentioned` flag first; text matching is a fallback
that recognises `@Soju`, `@**Soju**`, `@_**Soju**`, `@**Soju|12**` and a hand-typed
`@soju-bot`, for both the display name and the email local-part.

**A native slash command is never mention-gated.** `/help`, `/model`, `/stop`, `/approve`,
`/deny` and the plugin's own commands reach the gateway in any mode, including `oncall` in a
quiet topic with no engagement. The trigger gate is about conversation, and a command is not
conversation; the sender rate limit, the stream filter, group policy and stream policy still
apply to it, and an admin-only command is still the gateway's decision.

## Slash Commands

Messages starting with `/` are intercepted before they reach the agent. Plugin commands
never invoke the model themselves — they answer directly, or delegate to the agent when the
request needs judgement:

| Command | What it does | Use it when | Example |
|---------|--------------|-------------|---------|
| `/streams` | lists the streams the bot can see (or asks the AI to manage them) | you've forgotten what the bot is watching | `/streams` → a list of names |
| `/user` | looks up a Zulip user (or asks the AI) | you need someone's id or email | `/user niyaz@org.zulipchat.com` → name, id, status |
| `/pin` | stars the message you are replying to (or asks the AI) | a decision should stay easy to find | reply with `/pin` → ⭐ added |
| `/unpin` | un-stars it (or asks the AI) | the topic has moved on | `/unpin` → ⭐ removed |
| `/unlisten`, `/stop-listening` | ends a [sticky engagement](#sticky-engagement) early | the bot is still answering and shouldn't be | `/unlisten` → "Stopped listening in this topic" |

Add your own:

```python
from zulip.commands import register_command

@register_command("ping")
def _cmd_ping(args, chat_id, sender_email, sender_name):
    return "🏓 Pong!"          # user types /ping, sees 🏓 Pong!
```

> ⚠️ Do not register a name the gateway already owns, or the gateway command will never run.

## Progressive Activity Trace

> `/summarise every open PR` → the topic shows one message that updates as work proceeds,
> ending as `**Done**` with a note such as `replied in 18s`.

By default a topic only sees the final reply, so a run that takes a while is invisible.
With `ZULIP_ACTIVITY_TRACE=1`, the plugin keeps **one** bot-owned status message per run and
edits it in place:

```
**Working**

✓ terminal — 1234 ms
✓ read — 30 ms
… browser
```

Each finished tool call adds a line: `✓` succeeded, `✗` failed, `…` still running, with the
tool's duration as the detail. When the run ends the header becomes `**Done**`, `**Failed**`
or `**Cancelled**` and a closing note is appended:

```
**Done**

✓ terminal — 1234 ms
✓ read — 30 ms

replied in 18s
```
and is **never deleted**, so the topic keeps its audit trail. If the gateway restarts
mid-run (deploy, crash, OOM), the orphaned board is closed out at next start as
`⚪ **Cancelled** — run interrupted by a gateway restart`, so a stale "Working" cannot
outlive the process that posted it.

Steps come from two sources, and both may be on at once:

| Mode | Source | Notes |
|------|--------|-------|
| **A — automatic** | each finished tool call | Nothing to configure |
| **B — agent-authored** | the `zulip_progress` tool | For intent a tool call cannot reveal; offered only while the trace is on |

`ZULIP_TRACE_TOOL_MATCHER` picks which tools become checkpoints:

```bash
ZULIP_TRACE_TOOL_MATCHER=terminal          # only shell commands
ZULIP_TRACE_TOOL_MATCHER=terminal,read     # an allowlist
ZULIP_TRACE_TOOL_MATCHER='!browser'        # everything except one noisy tool
```

Names match exactly and case-insensitively; a `!` denial always wins.

**Coalescing keeps it a status board, not a metronome:** `ZULIP_TRACE_COALESCE_MS`
(default 400) folds bursts into one edit, `ZULIP_TRACE_MAX_RATE` (default 2/s) caps edits,
and an unchanged render spends no edit at all.

**Failure policy:** tracing is best-effort and never blocking — a failed post drops that
trace, a failed edit is logged and dropped (no retry loop), and a dead trace can never turn
a successful reply into a failed dispatch. Trace writes are credential-redacted, because
trace edits bypass the normal outbound secret guard.

Off by default, because it adds outbound writes.

## History-aware Context

> Someone asks *"didn't we hit this 502 before?"* → the bot answers with
> the earlier messages quoted in its prompt, instead of guessing.

Zulip is the only durable record of a topic, but by default the agent sees just the current
message plus whatever survived in its own memory. With `ZULIP_HISTORY_MODE` enabled, a
**bounded** slice of the current topic is added to the prompt as evidence:

```
[Topic history - recent messages quoted for context]
[Dana] same 502 on the auth service, it was the connection pool limit
[Bot] raised max_connections to 50 in commit 4f2c1ab
[Niyaz] it's back after the config revert
```

| Mode | Behaviour | When to use |
|------|-----------|-------------|
| `off` *(default)* | never harvest | you don't want the extra round-trip |
| `on-demand` | only when the message looks like a "do we know this?" question | **recommended** — no cost on ordinary turns |
| `always` | every inbound stream message carries the block | dense troubleshooting topics |

**Bounded on every axis:** `ZULIP_HISTORY_MAX_MESSAGES` (8), `ZULIP_HISTORY_WINDOW_HOURS`
(72) and `ZULIP_HISTORY_MAX_CHARS` (4000) cap the block, and selection keeps the *newest*
lines — a topic with months of history can't blow up the context window.

**Best-effort:** a slow harvest is logged and dropped behind a 2s timeout, so it can never
fail or stall a reply. **Streams and topics only** — DMs keep their isolated per-user
sessions.

## In-Channel Action Triggers

> The bot proposes a fix and ends with "say the word"; you react 👍 to
> *its* message; the agent proceeds in that same topic, with its reply and trace landing there.

```bash
ZULIP_REACTION_TRIGGERS='{"+1": "Proceed with the proposed step.", "check": "Ship it and open the PR."}'
```

React 👍 on the bot's own message in a monitored stream and the mapped instruction is
dispatched as a normal turn for **that same stream/topic session**.

**A reaction is a trigger, not an authorisation bypass.** The synthetic turn carries the
reacting human as its sender, so `ZULIP_DM_POLICY` / `ZULIP_GROUP_POLICY`, the allowlists,
the command gate and the rate limit all still apply to *them*. A stranger's reaction does
nothing.

| Rule | Why |
|------|-----|
| Only the bot's **own** messages by default | a reaction is an approval of the agent's proposal, not a licence to act on someone else's message (`ZULIP_REACTION_TRIGGER_ANY_MESSAGE=true` overrides) |
| The bot must be **subscribed** to the stream | Zulip only delivers `reaction` events to subscribers, and `message` events arrive anyway — so an unsubscribed stream fails **silently** |
| Streams only, and only monitored ones | DMs have no reaction surface in this flow |
| Fires **once** per (message, emoji, user) | taps, toggles and replayed events hit the on-disk dedupe store, so a restart cannot re-trigger work |
| **Audited** | each dispatch writes a `reaction_trigger` audit event |

Emoji names are the **Zulip API names**, not glyphs: 👍 is `+1`, 👀 is `eyes`. A name that
doesn't match a Zulip emoji never fires. With no map set, the `reaction` event type is not
even requested from Zulip.

Deliberately **not** supported: launching arbitrary named workflows or scripts. The trigger
is an instruction to the agent already in this conversation, so its blast radius equals
someone typing that sentence.

## Exec Approvals

Hermes stops before a dangerous command and asks the room. On Hermes `>= 0.21.3` the
Zulip adapter renders that prompt natively — one message of context and one zform
message whose buttons are **Allow Once / Allow Session / Always Allow / Deny**; on older
gateways the prompt is plain text with `/approve` and `/deny` instructions, and either
way the click or the typed command reaches the gateway (a slash command is never
mention-gated).

**Nobody answering is a refusal, and the bot says so.** Once the gateway's
`approvals.timeout` elapses the command does *not* run — on every host this plugin
supports. `ZULIP_APPROVAL_ON_TIMEOUT` decides what the *install promises* about that
silence, and therefore what the bot states and records:

| Value | What happens when nobody answers |
|-------|----------------------------------|
| `allow` *(default)* | Today's behaviour exactly: the gateway's own outcome stands and the bot adds nothing to the topic. |
| `deny` | The refusal is stated in the prompt's topic and recorded — one line saying the request was refused, plus an audit entry naming `timeout` as the decider. **The recommended profile sets this.** On hosts `>= 0.21.4` the gateway posts its own timeout notice, so the bot stays quiet instead of repeating it. |

Whichever value is set, a person's `/deny` is confirmed in the topic by the gateway, and
the bot does not duplicate it. Every resolved approval also lands in the audit log with
its choice, its decider (the person who clicked, or `timeout` / `policy` / `cancelled` /
`undelivered`) and the request id the plugin minted for the prompt, so "who let this run,
and did anyone?" is a lookup rather than a guess. This key is a policy statement, not a
second decision path: the buttons remain the only way to approve, and no setting can make
silence run a command.

**Who may decide.** The prompt lands in a *topic*, so on a shared work Zulip "anyone who
can read it" is a privilege escalation through a side channel: a coworker approves your bot
running a command on your host, with your credentials, and the audit names the coworker as
the decider of your bot.

| `ZULIP_APPROVAL_AUTHORITY` | What happens when someone decides |
|-------|-----------------------------------|
| `anyone` *(default)* | Today's behaviour exactly: anyone who can see the prompt may answer it. |
| `owner` | Only the bot owner may decide — the identity Zulip reports as the bot's `bot_owner_id`, or `ZULIP_OWNER_EMAIL` when you name it explicitly. Anyone else's `/approve` or `/deny` is refused: it is stated in the topic, audited with their identity, and **not counted**, so the prompt stays open for you and the timeout default still applies. **The recommended profile sets this.** If no owner can be resolved, nobody can decide — including you — and the bot says so in the topic and in the startup log, naming `ZULIP_OWNER_EMAIL` as the fix. |

A refusal is a rule, not a second UI: there is no approver list to configure, because a
set of approvers would recreate the problem it is meant to close.

## Sticky Engagement

> `@**hermes-bot** fix the typo in the README` → then, **without
> mentioning it again**, `and update the changelog too` → the bot answers both. Five
> minutes of silence ends it (`/unlisten` ends it immediately).

Re-mentioning a bot in every message is friction. With `ZULIP_ENGAGEMENT_MODE=sticky_topic`,
a mention opens a window in that topic during which follow-ups are answered without a
mention:

```bash
ZULIP_ENGAGEMENT_MODE=sticky_topic   # off (default) | sticky_topic
ZULIP_ENGAGEMENT_SCOPE=user          # user (default, only whoever mentioned it) | topic (anyone)
ZULIP_ENGAGEMENT_TTL_MINUTES=45      # idle window; 45 is the default
```

| Setting | Values | Example |
|---------|--------|-------------------|
| `ZULIP_ENGAGEMENT_MODE` | `off` *(default)*, `sticky_topic` | `off` = mention every time; `sticky_topic` = mention once |
| `ZULIP_ENGAGEMENT_SCOPE` | `user` *(default)*, `topic` | `user`: only your follow-ups; `topic`: anyone in the topic |
| `ZULIP_ENGAGEMENT_TTL_MINUTES` | default `45` | `5` = the window closes after 5 idle minutes |
| `ZULIP_ENGAGEMENT_EXPIRY_NOTICE` | `true` *(default)* | `true` = the bot posts "no longer listening" when the window lapses |
| `ZULIP_ENGAGEMENT_EXPIRY_SCAN_SECONDS` | default `30` | how often expiry is checked |

End it early with `stop listening`, `/unlisten` or `/stop-listening` in the topic.

**Bot traffic never keeps a window open.** A message whose sender is a bot — our own
send echoed back, or any address following Zulip's `<name>-bot@…` convention — is neither
answered *because* the topic is engaged nor allowed to refresh the idle TTL. Otherwise two
bots in one topic would keep each other's window open forever, outliving the human who
started the conversation. This is a loop-prevention invariant rather than a setting: no key
turns it off, so it cannot be re-enabled by accident. A bot that @mentions the bot
explicitly is a separate, deliberate act — mention gating is unchanged, it just does not
open a window of its own.

An invalid value here is logged and falls back safely (`off` / `user`) rather than
half-enabling the feature.

## Per-Session Queue

> In a shared topic, you ask for a long change; your teammate asks for a
> different one while it runs. Without this, their message is pushed *into* your running
> turn and redirects your work. With `ZULIP_SESSION_QUEUE=1`, theirs waits its turn.

A Zulip topic is **one conversation** with **one session**, so the bot works one request at
a time there — and the gateway's default is to steer a mid-run message into the running
turn. That is unfriendly to a shared room.

```bash
ZULIP_SESSION_QUEUE=1     # off by default
ZULIP_QUEUE_CAP=20        # how many may wait before a new one dispatches immediately
```

- A message arriving while a run is active for that topic (or DM) **waits** instead of
  steering it. **Separate topics are separate sessions and still run in parallel.**
- The waiting message gets an hourglass reaction — Zulip has no "queued input" surface — and
  the reaction disappears the moment its turn starts.
- Past the cap a message is dispatched **immediately rather than dropped**.
- Every transition is **audit-logged** as `session_queue_enqueue` / `session_queue_dequeue`
  (and `session_queue_overflow` past the cap) with the
  message id and queue depth, which is the durable proof the queue engaged.

Use separate topics for genuinely parallel work; use DMs for private work.

## Stream Watching

> You want the bot to know what `#general` has been discussing without it
> answering every line.

Two flags exist for this, and **they are alternatives — pick one**:

| Flag | What happens | Example |
|------|--------------|-------------------|
| `ZULIP_SOFT_GATE=1` | **every** monitored stream message is dispatched, tagged `addressed=true/false` in metadata, so the agent decides whether to speak | the bot sees the whole conversation and usually stays quiet |
| `ZULIP_OBSERVE_GROUP=1` | non-addressed messages are **not** dispatched; they're buffered per topic (20 msgs / 4000 chars / 200 topics) and quoted into the prompt the next time the bot **is** addressed | `@**hermes-bot** what did I miss?` → it answers from the buffer |

> ⚠️ **Do not enable both.** `soft_gate` dispatches everything, so messages never reach the
> drop path where observation happens — `ZULIP_OBSERVE_GROUP` becomes a no-op while
> `soft_gate` wins silently. They are mutually exclusive by construction.

The observed buffer arrives in the prompt under a clear label so the agent can tell quoted
context from the live message:

```
[Observed topic history - not addressed to you]
- Dana: I'm seeing a 502 on the auth service again
- Niyaz: it's the connection pool, bump max_connections
```

## Actionable Refs

> The agent writes `see https://github.com/owner/repo/pull/128` → if that
> PR exists you get a clickable `owner/repo#128` link; if it 404s, the URL is left exactly as
> written.

"I opened a PR" is readable but not *actionable*. This plugin validates GitHub references the
agent writes and turns the confirmed ones into labelled links:

```
before:  Shipped in https://github.com/owner/repo/pull/128 — CI is green
after:   Shipped in [owner/repo#128](https://github.com/owner/repo/pull/128) — CI is green
```

An explicit marker is also accepted, and validated the same way:

```
[[zulip_ref: https://github.com/owner/repo/actions/runs/12345 | CI is green]]
```

| Ref shape | Validated as |
|---|---|
| `github.com/<owner>/<repo>/pull/<n>` | a pull request |
| `github.com/<owner>/<repo>/issues/<n>` | an issue |
| `github.com/<owner>/<repo>/commit/<sha>` | a commit |
| `github.com/<owner>/<repo>/actions/runs/<id>` | an Actions run |

**This is always on** — there is no flag to set. It is safe to leave on because of how it
fails:

- **A bare URL that cannot be confirmed is left untouched** (a 404, a rate limit, a timeout,
  a malformed URL). Rendering never makes prose worse.
- **Only `https://github.com/...` is handled.** Anything else — internal hosts, lookalike
  domains such as `github.com.evil.com` — is rejected *before any network request*, and the
  API origin is a hardcoded `https://api.github.com`, so there is no configurable host to
  widen into an SSRF primitive.
- **No credentials are sent.** Validation is unauthenticated, so a private ref simply 404s
  and stays plain text. Outcomes are cached for ~10 minutes and at most 3 refs per message
  are validated, keeping a busy topic inside GitHub's 60/hour unauthenticated budget.
- **Best-effort** — each validation request is bounded by an 8s timeout and rendering never
  raises, so a slow or unreachable GitHub cannot fail or stall a send.

## Sending Files

> The agent saves `report.csv` to its workspace and sends it; the topic
> gets a clickable download link.

```python
from zulip.workspace import BotWorkspace

ws = BotWorkspace()
path = ws.save_text("report.csv", "id,value\n1,42\n")

await adapter.send(
    chat_id="dm:42",
    content="Here is your report:",
    media_files=[path]
)
```

Available methods: `save_text()`, `save_bytes()`, `save_json()`, `read_text()`,
`list_files()`, `clear()`. Temp files auto-delete after upload; path traversal and symlinks
are rejected.

## Environment Variable Reference

Set these in `~/.hermes/.env`. Credentials can also be provided by the setup wizard.
*This section is reference material — the features above are the place to start.*

### Required

| Variable | Example | Notes |
|----------|---------|-------|
| `ZULIP_API_KEY` | `abcd1234…` | bot API key from Zulip settings |
| `ZULIP_EMAIL` | `hermes-bot@your-org.zulipchat.com` | bot email |
| `ZULIP_SITE` | `https://your-org.zulipchat.com` | realm URL, `https://` only unless insecure HTTP is allowed |

### Access control

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_DM_POLICY` | `open` | `pairing` | `open` / `allowlist` / `pairing` / `disabled` |
| `ZULIP_ALLOWED_USERS` | *(empty)* | `dana@org.zulipchat.com` | comma-separated DM allowlist |
| `ZULIP_GROUP_POLICY` | `open` | `allowlist` | who may trigger the bot in streams |
| `ZULIP_GROUP_ALLOW_FROM` | *(empty)* | `dana@org.zulipchat.com` | comma-separated stream allowlist |
| `ZULIP_ALLOW_ALL_USERS` | `false` | `false` | disables authorization entirely — **dev only** |
| `ZULIP_MAX_MESSAGES_PER_MINUTE` | `60` | `10` | per-sender rate limit; `0` disables |

**Pairing mode:** a new user DMs the bot → the bot replies with a code that lasts 24 hours
and works once → an admin approves it → they can DM immediately.

```bash
python3 -m zulip.pairing list                              # who is waiting
python3 -m zulip.pairing approve PAIR-ABC123               # approve by code
python3 -m zulip.pairing approve them@org.zulipchat.com    # or by email
python3 -m zulip.pairing revoke them@org.zulipchat.com     # take access away
```

Pending requests are stored next to the allowlist, and the running gateway notices a change
on the next DM it handles — there is nothing to restart. `allowlist` mode is the simpler
choice when you do not want a code at all: it just reads `ZULIP_ALLOWED_USERS`.

### Stream triggers and addressing

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_CHATMODE` | `onmessage` | `oncall` | `onmessage` / `oncall` / `onchar` |
| `ZULIP_REQUIRE_MENTION` | `true` | `false` | require @mention in streams |
| `ZULIP_ONCHAR_PREFIXES` | `!,>` | `?` | prefixes that trigger `onchar` |
| `ZULIP_STREAMS` | `*` | `general,dev` | streams to monitor (`*` = all) |
| `ZULIP_STREAM_OVERRIDES` | *(empty)* | `{"bot lab": {"chatmode": "onmessage"}}` | per-stream chatmode overrides |
| `ZULIP_SOFT_GATE` | `false` | `1` | dispatch every stream message, tagged addressed/unaddressed — [see above](#stream-watching) |
| `ZULIP_OBSERVE_GROUP` | `false` | `1` | buffer non-addressed messages as topic context — **not with soft gate** |
| `ZULIP_HOME_CHANNEL` | *(empty)* | `573423` | default stream id for cron `deliver: zulip` |

### Sessions

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_TOPIC_SESSIONS` | `false` | `true` | give each topic its own session — rename-proof: sessions key on stable conversation ids, so renaming a topic continues its session; `/new` starts a fresh one |
| `ZULIP_DM_SESSION_TURN_LIMIT` | `20` | `0` | rotate a DM session after N turns; `0` disables |
| `ZULIP_SESSION_QUEUE` | `false` | `1` | hold a mid-run message behind the running turn |
| `ZULIP_QUEUE_CAP` | `20` | `5` | how many may wait before dispatching immediately |

### Exec approvals

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_APPROVAL_ON_TIMEOUT` | `allow` | `deny` | What an unanswered approval means: `allow` = today's behaviour (the gateway's refusal stands, the bot adds nothing); `deny` = also state the refusal in the topic and record `timeout` as the decider. The gateway owns the timeout and refuses either way — no setting makes silence run a command |
| `ZULIP_APPROVAL_AUTHORITY` | `anyone` | `owner` | Who may decide an exec approval: `anyone` = today's behaviour; `owner` = only the bot owner (`bot_owner_id`, or `ZULIP_OWNER_EMAIL`), with anyone else's decision refused, stated in the topic and audited, and the prompt left open for the owner. With no owner resolvable, nobody can decide |

### Sticky engagement

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_ENGAGEMENT_MODE` | `off` | `sticky_topic` | answer follow-ups without a mention |
| `ZULIP_ENGAGEMENT_SCOPE` | `user` | `topic` | `user` = only the mentioner; `topic` = anyone |
| `ZULIP_ENGAGEMENT_TTL_MINUTES` | `45` | `5` | idle minutes before the window lapses |
| `ZULIP_ENGAGEMENT_EXPIRY_NOTICE` | `true` | `false` | post a notice when it lapses |
| `ZULIP_ENGAGEMENT_EXPIRY_SCAN_SECONDS` | `30` | `10` | how often expiry is scanned |

### Status reactions

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_REACTIONS_ENABLED` | `true` | `false` | master switch for 👀/✅/⚠️ |
| `ZULIP_REACTION_START` | `eyes` | `hourglass` | reaction while working |
| `ZULIP_REACTION_SUCCESS` | `check_mark` | `tada` | reaction on success |
| `ZULIP_REACTION_ERROR` | `warning` | `x` | reaction on failure |
| `ZULIP_REACTION_CLEAR_ON_FINISH` | `true` | `false` | remove the status reaction when done |

### Action triggers

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_REACTION_TRIGGERS` | *(empty)* | `{"+1": "Proceed with the proposed step."}` | emoji name → instruction |
| `ZULIP_REACTION_TRIGGER_ANY_MESSAGE` | `false` | `true` | allow triggers on messages the bot did not author |

### Activity trace

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_ACTIVITY_TRACE` | `false` | `1` | master switch for the live status board |
| `ZULIP_TRACE_COALESCE_MS` | `400` | `1000` | fold bursty updates into one edit |
| `ZULIP_TRACE_MAX_RATE` | `2` | `5` | ceiling on edits per second |
| `ZULIP_TRACE_MAX_CONTENT` | `3500` | `2000` | trim older steps past this length |
| `ZULIP_TRACE_TOOL_MATCHER` | *(all tools)* | `terminal,!browser` | which tool calls become checkpoints |

### History

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_HISTORY_MODE` | `off` | `on-demand` | `off` / `on-demand` / `always` |
| `ZULIP_HISTORY_MAX_MESSAGES` | `8` | `20` | quoted messages per turn |
| `ZULIP_HISTORY_WINDOW_HOURS` | `72` | `168` | how far back to harvest |
| `ZULIP_HISTORY_MAX_CHARS` | `4000` | `8000` | cap on the quoted block |

### Output, security and limits

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_TEXT_CHUNK_LIMIT` | `10000` | `4000` | max chars per outbound message |
| `ZULIP_CHUNK_MODE` | `length` | `newline` | split by size, or on every newline |
| `ZULIP_MAX_MESSAGE_LENGTH` | `20000` | `5000` | hard cap before chunking; `0` disables. Truncated content gets a `[...message truncated]` marker |
| `ZULIP_RESPONSE_PREFIX` | *(empty)* | `🤖 ` | prepended to every reply |
| `ZULIP_BLOCK_SECRET_LEAKS` | `true` | `false` | refuse to send a message containing a host credential |
| `ZULIP_ALLOW_INSECURE_HTTP` | `false` | `true` | allow an `http://` or private `ZULIP_SITE` — the API key then travels unencrypted |
| `ZULIP_MEDIA_MAX_MB` | `5` | `20` | max inbound attachment size |
| `ZULIP_BLOCK_STREAMING` | `false` | `true` | experimental block streaming |

### Timeouts and legacy

| Variable | Default | Example | Notes |
|----------|---------|---------|-------|
| `ZULIP_CONNECT_TIMEOUT` | `30` | `10` | connection validation calls (seconds) |
| `ZULIP_READ_TIMEOUT` | `60` | `120` | read/get API calls |
| `ZULIP_SEND_TIMEOUT` | `90` | `120` | send/write API calls |
| `ZULIP_TYPING_DELAY_SECONDS` | *(unset)* | `5` | legacy typing duration; typing is now owned by the gateway's keep-typing loop |

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `zulip package not installed` | SDK missing from Hermes's env | `pip install "zulip>=0.9.0"` in that same environment |
| `No adapter available for zulip` | plugin failed to import | check the log for a syntax/import error; confirm the whole repo was installed |
| Bot silent in a stream | not subscribed, or trigger mode requires a mention | subscribe the bot in Stream settings; check `ZULIP_CHATMODE` |
| Bot replies to everything | `ZULIP_CHATMODE=onmessage` with `ZULIP_REQUIRE_MENTION=false` | switch to `oncall`, or set `ZULIP_REQUIRE_MENTION=true` |
| `Invalid or unsafe ZULIP_SITE` | `http://`, localhost or an IP | use an `https://` host, or opt in with `ZULIP_ALLOW_INSECURE_HTTP` |
| Reaction trigger does nothing | bot not subscribed to that stream | Zulip delivers `reaction` events only to subscribers — subscribe and retry |
| Sticky follow-ups ignored | window lapsed, or an invalid mode fell back to `off` | re-mention the bot; check the log for a rejected `ZULIP_ENGAGEMENT_MODE` |
| Messages held mid-run | `ZULIP_SESSION_QUEUE=1` working as intended | wait for the hourglass to clear; check `session_queue_dequeue` in the audit log |
| `ZULIP_OBSERVE_GROUP` seems dead | `ZULIP_SOFT_GATE` is also on | they are mutually exclusive — turn soft gate off |
| No activity trace | `ZULIP_ACTIVITY_TRACE` unset | tracing is opt-in; set it to `1` |
| Setup wizard shows instructions only | `setup_fn=interactive_setup` not passed to `register()` | reinstall the plugin from source |

More detail, including the gateway-compatibility matrix, lives in [AGENTS.md](AGENTS.md).

## Contributing

```bash
git clone https://github.com/YOU/zulip-hermes-integration.git
cd zulip-hermes-integration
bash scripts/setup-hooks.sh          # install the pre-push hook

# ... make changes ...

python3 -m pytest tests/             # the full unit suite
bash .githooks/pre-push              # checksums + syntax + manifest + tests
```

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for development setup, the checks CI runs and the
repo-specific rules (the updater manifest, `plugin.yaml` declarations, `checksums.txt`).

Then open a PR. `main` is protected: PR required, linear history, squash merge, and the
`zulip-bridge` GitHub Actions job must pass — the **Tests** badge at the top of this file is
that workflow's live status. No test *count* is written down here: a copied number is wrong the
moment the next test lands, and a published one needs a branch and a write-permission job to
stay true.

## Documentation

- **[AGENTS.md](AGENTS.md)** — the runtime guide the agent itself reads: addressing rules, metadata, injected context labels, troubleshooting
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — development setup, what CI runs, repo-specific rules
- **[SECURITY.md](SECURITY.md)** — threat model, credential handling, explicit non-guarantees
- **[SUPPORT.md](SUPPORT.md)** — where to ask for help
- **[docs/RELEASING.md](docs/RELEASING.md)** — release procedure
- **[CHANGELOG.md](CHANGELOG.md)** — release history

## Related

- [openclaw-zulip-bridge](https://github.com/niyazmft/openclaw-zulip-bridge) — the OpenClaw (TypeScript) sibling adapter in the same Zulip agent family
- [Hermes plugin docs](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters)
- [Zulip API documentation](https://zulip.com/api/)

## License

MIT License — see [LICENSE](LICENSE).
