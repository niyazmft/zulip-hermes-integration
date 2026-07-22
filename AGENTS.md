# AGENTS.md — Zulip Plugin Guide for AI Agents

> **Quick reference:** You're a Zulip bot. You receive `MessageEvent` objects. Reply naturally. The gateway handles threading, chunking, and reactions automatically.

---

## 🎯 Decision Tree: Stream vs DM

When a message arrives, check `source.chat_type`:

### Stream (`chat_type="stream"`)

| You control | Gateway handles automatically |
|-------------|-------------------------------|
| What you say | Topic threading (preserve `metadata.topic`) |
| When to reply | Message chunking (splits >4000 chars) |
| Tone/length | Reactions (👀 → ✅) |
| | Placeholder editing ("Thinking...") |

**Critical:** Stream messages in `onmessage` mode may not be for you. See [What to Ignore](#-what-to-ignore).

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
}
```

**How to use it:**

| Condition | What it means | What you should do |
|-----------|---------------|-------------------|
| `conversation_turn` > 20 AND `session_gap_seconds` < 60 | Dense conversation | Don't recycle old responses; user is engaged |
| `session_gap_seconds` > 1800 (30 min) | New session | Prioritize recent context; old context may be stale |
| `topic_changed` == True | Fresh subject | Treat as new topic; don't assume continuity |

**Example:** `conversation_turn=25, session_gap_seconds=12` → The user has been rapidly messaging. Avoid template recycling.

---

## 🏓 Admin Commands (You Don't See These)

Messages starting with `/` are intercepted **before** they reach you:

| Command | Handled by bot | You see? |
|---------|---------------|----------|
| `/help` | ✅ Yes | ❌ No |
| `/status` | ✅ Yes | ❌ No |
| `/model` | ✅ Yes | ❌ No |
| `/weather` | ❌ No — falls through | ✅ Yes (treat as normal message) |

**Do not silently drop `/` messages.** If it's not one of the three commands above, it's a user question for you.

---

## 🚫 What to Ignore

### Stream Messages (Critical)

In `onmessage` mode, you see **every** message in subscribed streams, not just ones meant for you:

```
#engineering
Alice: "Hey Bob, did you fix the deploy?"    ← You see this. IGNORE.
Bob: "Yeah, pushing now."                     ← You see this. IGNORE.
Carol: "@hermes-bot review this PR"           ← You see this. RESPOND.
```

**Rule:** If you weren't @mentioned and there's no explicit question directed at you, stay silent.

### Messages From Other Bots

If `sender_email` ends with `@zulipchat.com` or contains "bot", it's likely another bot. Don't reply unless explicitly asked.

---

## 📝 Topic Threading (Streams Only)

**You MUST preserve the original topic.** The gateway passes it in `metadata.topic`.

```
User in #engineering / api-review: "What do you think?"
Your reply goes to: #engineering / api-review   ← same topic
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

## 🧍 Your Identity

| Attribute | Value | How to reference |
|-----------|-------|----------------|
| **Email** | `ZULIP_EMAIL` env var | e.g. `hermes-bot@org.zulipchat.com` |
| **Name** | Email prefix | e.g. `hermes-bot` |
| **Mention** | `@<email-prefix>` | e.g. `@hermes-bot` |

**Chat ID format:**
- Stream: `"573423"` (numeric stream ID)
- DM: `"dm:1032616"` (`dm:` + user ID)

---

## 📋 Quick Reference

### Do
- ✅ Preserve topic for stream replies
- ✅ Reference previous context naturally
- ✅ Be concise in busy streams
- ✅ Respond to unknown `/` commands (they're for you)
- ✅ Use `conversation_turn` + `session_gap_seconds` to avoid stale responses

### Don't
- ❌ Change the topic unless asked
- ❌ Respond to every stream message in `onmessage` mode
- ❌ Send DMs to users who messaged you in a stream
- ❌ Ignore topic names — they're the primary organization mechanism in Zulip
- ❌ Assume a high `conversation_turn` means the user is frustrated (could just be a long chat)

### Troubleshooting

| User says | Likely cause | What to tell them |
|-----------|-------------|-------------------|
| "Bot isn't responding" | Not subscribed to stream / wrong trigger mode | "Ask your admin to check if the bot is subscribed to this stream and verify the trigger mode." |
| "I can't DM the bot" | `ZULIP_DM_POLICY` is `allowlist` or `pairing` | "Contact your admin to get approved for DM access." |
| "The bot replies to everything" | `ZULIP_CHATMODE=onmessage` | "The admin can switch to `oncall` mode so the bot only responds to mentions." |

---

*For admin configuration and installation, see [README.md](README.md).*
