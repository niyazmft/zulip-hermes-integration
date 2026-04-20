# Zulip Integration for Hermes Agent

Complete integration of Zulip as a first-class messaging platform in Hermes, matching Telegram's capabilities for bi-directional chat, cron deliveries, and automated messaging.

## Features

- ✅ Bi-directional chat via Zulip streams and DMs
- ✅ Cron job deliveries to Zulip streams
- ✅ Send message tool for automated notifications
- ✅ User authorization and RBAC
- ✅ Session tracking and context preservation
- ✅ Topic-based message organization

## Quick Start

### 1. Install Dependencies

```bash
pip install zulip
```

### 2. Create Zulip Bot

1. Go to your Zulip organization settings
2. Navigate to "Bots" → "Add a new bot"
3. Choose bot type: **Generic bot**
4. Copy the API key and note the bot's email address

### 3. Configure Environment Variables

Add to your `.env` file:

```bash
# Zulip credentials
ZULIP_API_KEY=your-bot-api-key
ZULIP_EMAIL=your-bot@niyaz.zulipchat.com
ZULIP_SITE=https://niyaz.zulipchat.com

# Authorization (comma-separated Zulip emails)
ZULIP_ALLOWED_USERS=user1032616@niyaz.zulipchat.com

# Optional: Home stream for cron deliveries
ZULIP_HOME_CHANNEL=573423
ZULIP_HOME_CHANNEL_NAME=general
```

### 4. Test Connection

```python
import zulip

client = zulip.Client(
    email="your-bot@niyaz.zulipchat.com",
    api_key="your-api-key",
    site="https://niyaz.zulipchat.com"
)

result = client.get_members()
print(result["result"])  # Should be "success"
```

## Architecture

### Components

```
hermes/
├── gateway/
│   ├── platforms/
│   │   └── zulip.py          # Core adapter
│   ├── config.py             # Platform enum + env loading
│   └── run.py                # Adapter factory + auth
├── tools/
│   └── send_message_tool.py  # Zulip send implementation
├── cron/
│   └── scheduler.py          # Delivery platform map
└── toolsets.py               # hermes-zulip toolset
```

### Message Flow

```
Zulip Stream/DM
    ↓
ZulipAdapter._listen_for_events()
    ↓
SessionSource (with stream_id, topic)
    ↓
Gateway message dispatch
    ↓
AIAgent conversation loop
    ↓
Response via ZulipAdapter.send()
```

## Usage

### Chat with Hermes

Send a message to the bot in Zulip. The bot will:
- Listen to messages in subscribed streams
- Respond to DMs
- Filter self-messages to prevent loops
- Authorize users via `ZULIP_ALLOWED_USERS`

### Cron Deliveries

```python
from hermes_tools import cronjob

# Daily project pulse to Zulip stream
cronjob(
    action="create",
    prompt="Check Linear for new issues and summarize",
    schedule="0 9 * * *",
    deliver="zulip:573423",  # Stream ID
    name="Daily Pulse"
)
```

### Send Message Tool

```python
from hermes_tools import send_message

# Send to stream
send_message(
    platform="zulip",
    chat_id="573423",  # Stream ID
    message="Hello from Hermes!",
    extra={"topic": "general"}
)

# Send DM
send_message(
    platform="zulip",
    chat_id="dm:12345",  # User ID
    message="Private notification"
)
```

### Chat ID Format

| Type | Format | Example |
|------|--------|---------|
| Stream message | Stream ID (string) | `"573423"` |
| Private message | `dm:` + User ID | `"dm:12345"` |
| Topic | `extra` parameter | `{"topic": "general"}` |

## Configuration Reference

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ZULIP_API_KEY` | ✅ | Bot API key from Zulip settings |
| `ZULIP_EMAIL` | ✅ | Bot email address |
| `ZULIP_SITE` | ✅ | Zulip organization URL |
| `ZULIP_ALLOWED_USERS` | ✅ | Comma-separated authorized user emails |
| `ZULIP_HOME_CHANNEL` | ❌ | Default stream ID for cron deliveries |
| `ZULIP_HOME_CHANNEL_NAME` | ❌ | Default topic for cron deliveries |
| `ZULIP_ALLOW_ALL_USERS` | ❌ | Set to `true` to skip authorization |

### Gateway Config (Optional)

```yaml
gateway:
  platforms:
    zulip:
      enabled: true
      api_key: your-bot-api-key
      extra:
        email: your-bot@niyaz.zulipchat.com
        site: https://niyaz.zulipchat.com
      home_channel:
        platform: zulip
        chat_id: "573423"
        name: general
```

## Examples

See the `examples/` directory for:
- Basic chat bot setup
- Cron job configurations
- Automated notification scripts
- RBAC policies

## Troubleshooting

| Error | Solution |
|-------|----------|
| "zulip package not installed" | `pip install zulip` |
| "Zulip credentials incomplete" | Check all three required env vars |
| Messages not received | Verify bot subscribed to stream |
| Cron delivery fails | Verify stream ID is numeric |
| Authorization failed | Add user email to `ZULIP_ALLOWED_USERS` |

## License

MIT License - See LICENSE file for details.

## Credits

Built for the Hermes Agent framework. Original integration by Niyaz (2026-04-20).
