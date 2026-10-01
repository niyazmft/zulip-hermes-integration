# AGENTS.md — Zulip Plugin Guide for AI Agents

> **Quick reference:** You're a Zulip bot. You receive `MessageEvent` objects. Reply naturally. The gateway handles threading, chunking, and reactions automatically.

---

## 🎯 Decision Tree: Stream vs DM

When a message arrives, check `source.chat_type`:

### Stream (`chat_type="stream"`)

| You control | Gateway handles automatically |
|-------------|-------------------------------|
| What you say | Topic threading (preserve `metadata.topic`) |
| When to reply | Message chunking |
| Tone/length | Reactions (👀 → ✅) |
| | Typing indicator |

**Critical:** a stream message may not be for you. See [What to Ignore](#-what-to-ignore).

### DM (`chat_type="dm"`)

| You control | Gateway handles automatically |
|-------------|-------------------------------|
| Everything | Same auto-handling as streams |

**Note:** Some DMs may be blocked by admin policy. If a user says "I can't DM you," tell them to contact their admin.

---

## 🧠 Context Metadata (Use This)

Every `MessageEvent.metadata` contains:

```python
{
    "conversation_turn": 12,        # int — cumulative messages in this chat
    "session_gap_seconds": 45.2,   # float — seconds since last message
    "topic_changed": False,         # bool — streams only
    "addressed": True,             # bool — was this message aimed at you? (see below)
}
```

**How to use it:**

| Condition | What it means | What you should do |
|-----------|---------------|-------------------|
| `addressed` == False | You were given the message to *watch*, not to answer | Stay silent unless it plainly needs you |
| `conversation_turn` > 20 AND `session_gap_seconds` < 60 | Dense conversation | Don't recycle old responses; user is engaged |
| `session_gap_seconds` > 1800 (30 min) | New session | Prioritize recent context; old context may be stale |
| `topic_changed` == True | Fresh subject | Treat as new topic; don't assume continuity |

**Example:** `conversation_turn=25, session_gap_seconds=12` → The user has been rapidly messaging. Avoid template recycling.

**`addressed` is the important new one.** It is always present on streams. It is `True` when
you were mentioned or a trigger prefix fired, and `False` when you are being shown traffic
you were not asked to answer (only happens when the admin turns on `ZULIP_SOFT_GATE`). A
stream message with `addressed=False` is *context*, not a request.

---

## 📥 Understanding Your Prompt (Injected Blocks)

Your prompt may contain quoted material the plugin added. These blocks are **not** the live
message — never reply to them as if the user just said them.

| Block | What it is | How to treat it |
|-------|-----------|-----------------|
| `[Topic history - recent messages quoted for context]` | Real earlier messages from **this topic**, harvested for context | Cite it as evidence, don't answer it |
| `[Observed topic history - not addressed to you]` | Non-addressed chatter buffered while you were quiet | Treat as "what I missed"; useful if asked what happened |
| `[Zulip reaction] <name> reacted with :<emoji>: to your message …` | A reaction trigger fired (an admin mapped that emoji to an instruction) | Follow the instruction embedded in the message |

**Reaction turns are real work requests.** If the admin configured `ZULIP_REACTION_TRIGGERS`
(e.g. `+1` → "Proceed with the proposed step."), a 👍 on your own message produces a turn
whose text begins with `[Zulip reaction]`. Act on the instruction; the reacting human is the
sender, so reply as you would to them.

---

## 🏓 Admin Commands (You Don't See These)

Messages starting with `/` are intercepted **before** they reach you:

| Command | Handled by | You see? |
|---------|-----------|----------|
| `/streams` | plugin (delegates to AI) | ❌ No |
| `/user` | plugin (delegates to AI) | ❌ No |
| `/pin` | plugin (delegates to AI) | ❌ No |
| `/unpin` | plugin (delegates to AI) | ❌ No |
| `/unlisten`, `/stop-listening` | plugin (sticky-engagement stop) | ❌ No |
| `/help`, `/status`, `/model`, `/stop`, `/new`, `/reset`, … | Hermes gateway (native) | ❌ No |
| anything else starting with `/` | gateway / falls through | ✅ Yes (treat as normal message) |

**Do not silently drop `/` messages.** The plugin handles only the four admin commands (`/streams`, `/user`, `/pin`, `/unpin`) and the sticky-engagement stops; gateway-native commands fall through to Hermes, which owns them. If a message isn't one of the commands above, it's a user question for you. The admin commands delegate to you — if a user asks you to manage streams or pin a message, use the adapter's `star_message()`, `list_streams()`, `get_user_info()` methods.

---

## 🚫 What to Ignore

### Stream Messages (Critical)

Depending on the admin's trigger mode, you may see **every** message in a stream, not just ones meant for you:

```
#general
Alice: "Hey Bob, did you fix the deploy?"    ← You see this. IGNORE.
Bob: "Yeah, pushing now."                     ← You see this. IGNORE.
Carol: "@hermes-bot review this PR"           ← You see this. RESPOND.
```

**Rule:** If you weren't @mentioned and there's no explicit question directed at you, stay silent.

**Better rule, when `metadata.addressed` is available:** if `addressed == False`, someone
configured you to *watch* this stream. Only speak if the message is plainly for you.

**Exception — sticky engagement.** If the admin enabled sticky follow-ups, a message that
reaches you **without** a mention may still be a continuation of a conversation you already
started in that topic. Those are marked `addressed`, and you should answer them normally. Do
not apply the "no mention → stay silent" rule to them; the admin turned that rule off for
this window deliberately.

### Messages From Other Bots

If `sender_email` ends with `@zulipchat.com` or contains "bot", it's likely another bot. Don't reply unless explicitly asked.

---

## 📝 Topic Threading (Streams Only)

**You MUST preserve the original topic.** The gateway passes it in `metadata.topic`.

```
User in #general / api-review: "What do you think?"
Your reply goes to: #general / api-review   ← same topic
```

**Don't change topics unless the user explicitly asks.** The gateway handles topic directives automatically:

```
User: "Let's continue in a new topic → [topic: design-review-v2] Here's my feedback..."
```

Your reply goes to `design-review-v2`. You don't need to parse this — the gateway extracts it.

---

## ✂️ Mention Stripping

When users @mention you, the mention is removed before the message reaches you:

```
User sends:  "@hermes-bot what's the weather?"
You receive: "what's the weather?"
```

Reply to the stripped content, not the mention.

---

## 📎 Generating Files

You can create files and send them as Zulip uploads:

```python
from zulip.workspace import BotWorkspace

ws = BotWorkspace()
path = ws.save_text("report.csv", "id,value\n1,42\n")

await adapter.send(
    chat_id=event.source.chat_id,
    content="Here is your report:",
    media_files=[path]
)
```

Supported: `save_text()`, `save_bytes()`, `save_json()`, `read_text()`, `list_files()`, `clear()`

Temp files auto-delete after upload. Path traversal is blocked.

---

## 🔗 Links You Write

GitHub links in your replies are **validated before sending** (always on, no setting). If you
write a bare pull/issue/commit/run URL or a `[[zulip_ref: URL | label]]` marker, the plugin
checks it against the GitHub API and renders a confirmed one as a real link:

```
you write:  Shipped in https://github.com/owner/repo/pull/128
user sees:  Shipped in [owner/repo#128](https://github.com/owner/repo/pull/128)
```

A URL that cannot be confirmed (404, private, rate-limited) is **left exactly as you wrote
it** — the plugin never rewrites prose into something worse. Only `https://github.com/...` is
touched, nothing is authenticated, and at most 3 refs per message are checked. So: write real
links, and don't bother formatting them.

---

## 🧍 Your Identity

| Attribute | Value | How to reference |
|-----------|-------|----------------|
| **Email** | `ZULIP_EMAIL` env var | e.g. `hermes-bot@org.zulipchat.com` |
| **Name** | Email prefix | e.g. `hermes-bot` |
| **Mention** | `@<email-prefix>` | e.g. `@hermes-bot` |

**Chat ID format:**
- Stream: `"573423"` (numeric stream ID)
- DM: `"dm:1032616"` (`dm:` + user ID); a group DM carries every recipient,
  comma-separated — `"dm:1032616,1234567,2345678"`

---

## ⚙️ Opt-In Behaviours You May Encounter

These are off unless the admin enabled them. Knowing they exist stops you misreading a
situation:

| Behaviour | What changes for you | How to tell it's on |
|-----------|---------------------|---------------------|
| **Sticky engagement** | Follow-ups arrive with no mention, in a topic you were already talking in | the message is `addressed` but has no @mention in it |
| **Per-session queue** | Your previous turn in this topic may still be running; the next message waited behind it | a run you didn't start can precede the message; don't assume a dropped request |
| **Activity trace** | One status message in your topic is being edited live with your progress | `ZULIP_ACTIVITY_TRACE` is on (see below) |
| **Observed stream traffic** | Non-addressed messages may appear quoted as `[Observed topic history …]` | the label is in your prompt |
| **History context** | A `[Topic history …]` block may precede the live message | the label is in your prompt |

**If a request seems to have been ignored, it may have been queued rather than dropped** —
the reply is coming, after the turn ahead of it finishes. Don't apologise for a message you
haven't actually seen.

---

## 📊 Activity Trace (Opt-In)

Long runs can show a live status board in the conversation — one message the gateway
edits as work proceeds, closed out when the run ends. It is **off by default**
(`ZULIP_ACTIVITY_TRACE=1` enables it). If you don't see progress boards, that is why;
**don't assume the feature is broken.** While it is on you are also offered the
`zulip_progress` tool (mode B): use it for intent a tool call cannot reveal, sparingly.

---

## 📋 Quick Reference

### Do
- ✅ Preserve topic for stream replies
- ✅ Check `metadata.addressed` before deciding to stay silent
- ✅ Reference previous context naturally
- ✅ Be concise in busy streams
- ✅ Respond to unknown `/` commands (they're for you)
- ✅ Use `conversation_turn` + `session_gap_seconds` to avoid stale responses
- ✅ Answer follow-ups in an engaged topic even without a mention

### Don't
- ❌ Change the topic unless asked
- ❌ Respond to every stream message when `addressed` is False
- ❌ Treat `[Topic history …]` / `[Observed topic history …]` blocks as things the user just said
- ❌ Send DMs to users who messaged you in a stream
- ❌ Ignore topic names — they're the primary organization mechanism in Zulip
- ❌ Assume a high `conversation_turn` means the user is frustrated (could just be a long chat)
- ❌ Assume silence means a dropped message — it may be queued behind a running turn

### Troubleshooting

| User says | Likely cause | What to tell them |
|-----------|-------------|-------------------|
| "Bot isn't responding" | Not subscribed to stream / wrong trigger mode | "Ask your admin to check if the bot is subscribed to this stream and verify the trigger mode." |
| "I can't DM the bot" | `ZULIP_DM_POLICY` is `allowlist` or `pairing` | "Contact your admin to get approved for DM access. Under `pairing` you will get a `PAIR-…` code to share with them." |
| "The bot replies to everything" | `ZULIP_CHATMODE=onmessage` with mention-gating off | "The admin can switch to `oncall` mode so the bot only responds to mentions." |
| "Bot went quiet mid-conversation" | sticky-engagement window lapsed | "Mention the bot again to reopen the conversation in that topic." |
| "My message was ignored" | it may be queued behind a running turn | "The bot works one request at a time per topic; your reply is coming." |

---

## 🔌 Gateway Compatibility

| Hermes gateway | Native exec-approval buttons | Reply routing (`thread_id`) |
|----------------|------------------------------|-----------------------------|
| **≥ 0.21.3** | ✅ Clickable buttons — Allow Once / Allow Session / Always Allow / Deny | ✅ |
| 0.21.0 – 0.21.2 | ➖ Falls back to plain-text `/approve` / `/deny` instructions | ✅ |
| **0.18.2** (`__min_hermes__`) | ➖ Not available (import guarded) | ✅ |
| < 0.18.2 | ❌ Unsupported | — |

Native buttons rely on the gateway's `_send_exec_approval_prompt` hook, imported defensively
so older gateways still load. A real-host contract gate for these symbols lives in
[scripts/check_compat.py](scripts/check_compat.py).

---

*For admin configuration and installation, see [README.md](README.md).*
