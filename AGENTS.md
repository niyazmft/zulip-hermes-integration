# Zulip Platform Agent Guide

## Your Identity on Zulip

You are a bot account on a Zulip server. Your identity is determined by:

| Attribute | Source | Example |
|-----------|--------|---------|
| **Bot email** | `ZULIP_EMAIL` env var | `hermes-bot@org.zulipchat.com` |
| **Bot name** | Derived from email prefix | `hermes-bot` |
| **Mention handle** | `@<email-prefix>` | `@hermes-bot` |

Users can address you in three ways:
1. **DM (private message)** — Only you and the user can see it
2. **Stream @mention** — Public stream message with `@your-bot-name`
3. **Stream trigger** — Depending on `ZULIP_CHATMODE`, messages may or may not reach you

## How Zulip Conversations Work

### Streams and Topics

Zulip organizes conversations into **streams** (like channels) and **topics** (like threads). Every stream message has a topic.

```
#engineering stream
├── deploy-issue    ← topic
├── api-review      ← topic
├── daily-standup     ← topic
```

**Critical for replies:** When responding in a stream, you MUST preserve the original topic so the conversation stays threaded. The gateway passes you the topic in `metadata={"topic": "..."}`.

### Chat ID Format

| Type | Format | Example | Notes |
|------|--------|---------|-------|
| Stream | Numeric stream ID | `"573423"` | Reply with same topic |
| DM | `dm:` + user ID | `"dm:1032616"` | One-on-one private |

## Conversation Context (MessageEvent)

When a message arrives, you receive:

```python
MessageEvent(
    text="the message content",
    message_type=MessageType.TEXT,
    source=Source(
        chat_id="573423" or "dm:1032616",
        chat_name="#engineering" or "Alice Smith",
        chat_type="stream" or "dm",
        user_id="alice@org.com",
        user_name="Alice Smith",
    ),
    message_id="12345",
    metadata={
        "topic": "api-review",       # stream messages only
        "stream_id": 573423,         # stream messages only
        "user_id": 1032616,          # DM only
        "user_email": "alice@org.com",  # DM only
    },
)
```

## How Users Reach You

### Trigger Modes (configured by admin)

| Mode | Behavior | When you're notified |
|------|----------|----------------------|
| `onmessage` | All stream messages | Every message in subscribed streams |
| `oncall` | Mention only | Only `@your-bot-name` mentions |
| `onchar` | Prefix trigger | Messages starting with `>`, `!`, or `@your-bot-name` |

**If `ZULIP_REQUIRE_MENTION=true`:** Even in `oncall`/`onchar` mode, users MUST @mention you or use the prefix. In `onmessage` mode, mentions are not required.

### DM Behavior

All DMs reach you regardless of trigger mode or mention settings. No gating applies to private messages.

## Your Response Behavior

### 1. Preserve Topic Threading

When replying to a stream message, the gateway automatically preserves the topic from the original message. **Do not change the topic unless explicitly asked.**

```
User in #engineering / api-review: "What do you think of this design?"
Your reply goes to: #engineering / api-review   ← same topic
```

### 2. Topic Directives (User-Initiated Changes)

If a user wants to move the conversation to a new topic, they can include a directive:

```
User: "Let's continue in a new topic → [topic: design-review-v2] Here's my feedback..."
```

You don't need to handle this — the gateway extracts the directive and routes your reply to the new topic automatically.

### 3. Mention Stripping

When users @mention you (`@hermes-bot`), the mention is automatically stripped from the message text before it reaches you. You only see:

```
User sends: "@hermes-bot what's the weather?"
You receive: "what's the weather?"
```

### 4. Message Length

Your responses can be up to **10,000 characters**. Long responses are automatically chunked into multiple messages by the gateway. You don't need to handle this.

### 5. Reactions

The gateway shows emoji reactions while processing:
- 👀 when you start thinking
- ✅ when you respond successfully
- ⚠️ if an error occurs

These are automatic — you don't control them.

### 6. Typing Indicators

The gateway shows a typing indicator while you generate a response. This is automatic.

### 7. Placeholder Editing

By default, the gateway sends a "🤔 Thinking..." placeholder message when you start processing, then edits it with your final response. This creates a smoother UX for slow responses.

- **Streams:** Placeholder is sent to the same topic as the original message
- **DMs:** Placeholder is sent to the user directly

If the admin sets `ZULIP_EDIT_PLACEHOLDER=false`, placeholder editing is disabled and you reply directly without any placeholder.

**Edge case:** If you error out while generating a response, the placeholder is updated to "❌ Error — could not generate response" so the user knows something went wrong rather than seeing a frozen "Thinking..." message.

**Edge case:** If you error out while generating a response, the placeholder is updated to "❌ Error — could not generate response" so the user knows something went wrong rather than seeing a frozen "Thinking..." message.

### 8. File Generation & Attachments

You can generate files (reports, CSVs, JSON dumps, etc.) and send them as Zulip uploads. The user receives a clickable link in your message.

**How to generate and send a file:**

```python
from zulip.workspace import BotWorkspace

# Create a workspace (sandboxed directory under /tmp)
ws = BotWorkspace()

# Generate content
path = ws.save_text("report.csv", "id,value\n1,42\n")

# Send with your message
await adapter.send(
    chat_id="dm:42",
    content="Here is your report:",
    media_files=[path]
)
```

**Supported operations:**
- `ws.save_text("file.txt", "content")` — UTF-8 text files
- `ws.save_bytes("file.bin", b"\x00...")` — binary files
- `ws.save_json("data.json", {"key": "value"})` — JSON with indentation
- `ws.read_text("file.txt")` — read back a file
- `ws.list_files()` — see what's in your workspace
- `ws.clear()` — delete everything in workspace

**Auto-cleanup:** Local temp files are automatically deleted after upload. The workspace also auto-prunes files older than 1 hour on every save.

**Security:** Only files under `/tmp` or `HERMES_DATA_DIR` can be uploaded. Path traversal attacks (e.g., `../../etc/passwd`) are rejected.

## Platform-Specific Etiquette

### Do
- Keep stream replies in the original topic unless asked to change
- Be concise in busy streams — long messages are fine but chunking may split them
- Reference previous context naturally since you see the full conversation thread

### Don't
- Assume all stream messages are for you (in `onmessage` mode, you see everything)
- Ignore topic names — they're the primary organization mechanism in Zulip
- Send DMs to users who messaged you in a stream (reply in the stream unless privacy needed)

## Metadata Reference

### Stream Message Metadata
```json
{
  "topic": "api-review",
  "stream_id": 573423
}
```

### DM Metadata
```json
{
  "user_id": 1032616,
  "user_email": "alice@org.com"
}
```

## Troubleshooting for Agents

If you receive a message but the user seems confused, possible causes:
- **Bot not subscribed:** The bot must be subscribed to a stream to see its messages (admins do this in Zulip UI)
- **Authorization:** If `ZULIP_ALLOWED_USERS` is set and the user's email isn't in it, their messages won't reach you
- **Trigger mode mismatch:** User might be sending stream messages that don't match the configured trigger

## For Admins (Configuration)

This plugin is configured via environment variables in `~/.hermes/.env`:

```bash
ZULIP_API_KEY=your-bot-api-key
ZULIP_EMAIL=hermes-bot@org.zulipchat.com
ZULIP_SITE=https://org.zulipchat.com
ZULIP_ALLOWED_USERS=alice@org.com,bob@org.com
```

The plugin is installed by copying files to `~/.hermes/plugins/zulip/` and running `hermes plugins enable zulip`.

**Important:** The `zulip` Python SDK must be installed manually (`pip install "zulip>=0.9.0"`) — Hermes does not auto-install plugin dependencies.

---

*This guide is for AI agents consuming the Zulip platform plugin. For developer documentation, see README.md.*