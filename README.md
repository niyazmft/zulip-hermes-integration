# 📬 Zulip Plugin for Hermes Agent

[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Hermes](https://img.shields.io/badge/hermes-%3E%3D0.18.2-orange)](https://hermes-agent.nousresearch.com)

A [Hermes Agent](https://hermes-agent.nousresearch.com) gateway plugin that adds **Zulip** as a first-class messaging platform. Chat with Hermes via Zulip **streams** (with topic-aware threading) and **DMs**.

> ⚠️ **Prerequisite:** You must manually install the `zulip` Python SDK before using this plugin:
> ```bash
> pip install "zulip>=0.9.0"
> ```
> Hermes does **not** auto-install plugin dependencies.

## Features

- ✅ Bi-directional chat via Zulip **streams** (with automatic topic threading) and **DMs**
- ✅ **"Thinking..." placeholder** — shows users the bot is working, then edits with the final response. Works for both streams and DMs; supports FIFO queue for concurrent messages.
- ✅ **Context-mitigation metadata** — tracks `conversation_turn`, `session_gap_seconds`, and `topic_changed` to help the upstream AI agent avoid stale template recycling.
- ✅ **Bot workspace** — AI agents can generate files (reports, CSVs, JSON) in a sandboxed workspace and send them as Zulip uploads. Auto-cleans temp files after upload.
- ✅ **Self-update via Git** — keep the plugin directory as a git clone and `git pull` to update.
- ✅ User authorization via email allowlist
- ✅ Interactive onboarding via `hermes gateway setup`
- ✅ Zero core code changes — pure plugin architecture

## Table of Contents

- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Architecture](#architecture)
- [Environment Variables](#environment-variables)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## Installation

### Prerequisites

- Python 3.8+
- Hermes Agent ≥ v0.18.2
- **Zulip SDK** (`zulip>=0.9.0`): **must be installed manually**
- A Zulip bot account ([create one here](https://zulipchat.com/help/add-a-bot))

> ⚠️ **Critical:** The `zulip` Python package is a **runtime dependency** that Hermes does **not** install automatically. You must install it yourself in the same Python environment as Hermes:
>
> ```bash
> pip install "zulip>=0.9.0"
> ```
>
> If you are running Hermes inside a Docker container, ensure `zulip` is either baked into the image (`RUN pip install zulip` in the Dockerfile) or auto-installed on container startup. Manual `pip install` inside a running container will be lost on restart.

### Option A: Git Clone (Recommended for Easy Updates)

This keeps the plugin as a proper Git repository so you can `git pull` to update:

```bash
# 1. Install the Zulip SDK (REQUIRED — not automatic)
pip install "zulip>=0.9.0"

# 2. Clone directly into the Hermes plugin directory
mkdir -p ~/.hermes/plugins
rm -rf ~/.hermes/plugins/zulip  # remove old files if any
git clone https://github.com/niyazmft/zulip-hermes-integration.git ~/.hermes/plugins/zulip

# 3. Enable the plugin
hermes plugins enable zulip
```

**Future updates:**
```bash
cd ~/.hermes/plugins/zulip
git pull origin main
hermes gateway restart
```

### Option B: Manual Copy (One-time Install)

```bash
# 1. Install the Zulip SDK (REQUIRED — not automatic)
pip install "zulip>=0.9.0"

# 2. Install the plugin into Hermes
mkdir -p ~/.hermes/plugins/zulip
cp zulip/__init__.py zulip/adapter.py zulip/plugin.yaml ~/.hermes/plugins/zulip/
hermes plugins enable zulip
```

> ⚠️ **Note:** Option B does not support easy updates. You must manually re-copy files every time the plugin changes.

### Option C: Bundled Plugin (Containers / System-wide)

```bash
HERMES_PATH=$(python3 -c "import hermes_cli; print(hermes_cli.__path__[0])")
mkdir -p "$HERMES_PATH/../plugins/platforms/zulip"
cp zulip/__init__.py zulip/adapter.py zulip/plugin.yaml "$HERMES_PATH/../plugins/platforms/zulip/"
```

## Configuration

### Interactive Setup (Recommended)

```bash
hermes gateway setup
```

Select **📬 Zulip** from the menu. The wizard will prompt for:
- Zulip site URL (e.g. `https://your-org.zulipchat.com`)
- Bot email address
- Bot API key (password-masked)
- Allowed users (optional)

Values are saved to `~/.hermes/.env` automatically.

Values are saved to `~/.hermes/.env` automatically.

### Manual Configuration

Add to `~/.hermes/.env`:

```bash
ZULIP_API_KEY=your-bot-api-key
ZULIP_EMAIL=your-bot@niyaz.zulipchat.com
ZULIP_SITE=https://niyaz.zulipchat.com
ZULIP_ALLOWED_USERS=your-email@niyaz.zulipchat.com
```

Then add to `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    zulip:
      enabled: true
```

### Subscribe Bot to Streams

By default, the bot only sees DMs and @-mentions. To receive all messages in a stream:

1. Go to **Stream settings → Subscribers**
2. Add your bot

## Usage

### Start the Gateway

```bash
hermes gateway
```

Send a message to the bot in Zulip (DM or subscribed stream). The bot will respond via the same channel, preserving the topic for stream messages.

## Architecture

```
Zulip Stream/DM
    ↓
ZulipAdapter._listen_for_events()   # Event queue long-polling
    ↓
MessageEvent (with topic metadata)
    ↓
Gateway session → AIAgent
    ↓
ZulipAdapter.send() → Zulip REST API
```

The adapter uses Zulip's **event queue API** for inbound messages and wraps all synchronous SDK calls with `asyncio.to_thread()` to keep the gateway event loop responsive.

### Chat ID Format

| Type | Format | Example |
|------|--------|---------|
| Stream | Numeric stream ID | `"573423"` |
| Private message | `dm:` + user ID | `"dm:1032616"` |

For stream messages, the adapter caches the last seen **topic** per stream and uses it for replies, so conversations stay threaded.

## Context-Mitigation Metadata

Every incoming message includes metadata that helps the upstream AI agent manage conversation context and avoid stale responses:

| Field | Type | Meaning |
|-------|------|---------|
| `conversation_turn` | `int` | Cumulative message count in this chat |
| `session_gap_seconds` | `float` | Seconds since the last message in this chat |
| `topic_changed` | `bool` | `true` if the stream topic changed since the last message |

**Agent guidance:** When `conversation_turn` is high (>20) AND `session_gap_seconds` is low, the conversation is dense — guard against stale template recycling. When `topic_changed` is `true`, treat this as a fresh subject within the same stream.

## Bot Workspace & File Attachments

Bots can generate files (reports, CSVs, JSON, etc.) in a sandboxed workspace and send them as Zulip uploads:

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

The file is uploaded to Zulip's `/user_uploads` and rendered as a clickable Markdown link in the message. Local temp files are **auto-deleted after upload** to prevent disk bloat.

**Workspace features:**
- `save_text(filename, content)` — write UTF-8 text
- `save_bytes(filename, content)` — write raw bytes
- `save_json(filename, data)` — serialize JSON with indentation
- `read_text(filename)` / `read_bytes(filename)` — read back files
- `list_files()` — list all files in workspace
- `clear()` — delete everything in workspace
- Auto-prunes files older than 1 hour on every save (configurable via `ttl=`)
- Path traversal attacks are rejected

### Sending Existing Files

You can also pass any file path to `media_files`:

```python
await adapter.send(chat_id, "See attached", media_files=["/path/to/data.pdf"])
```

Only files under `/tmp` or `HERMES_DATA_DIR` are accepted — path traversal is blocked.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ZULIP_API_KEY` | ✅ | Bot API key from Zulip settings |
| `ZULIP_EMAIL` | ✅ | Bot email address |
| `ZULIP_SITE` | ✅ | Zulip organization URL |
| `ZULIP_ALLOWED_USERS` | ❌ | Comma-separated authorized user emails |
| `ZULIP_ALLOW_ALL_USERS` | ❌ | Set `true` to disable authorization (dev only) |
| `ZULIP_EDIT_PLACEHOLDER` | ❌ | Set `false` to disable "Thinking..." placeholder editing |
| `ZULIP_MEDIA_MAX_MB` | ❌ | Max inbound attachment size in MB (default: 5) |
| `ZULIP_REACTIONS_ENABLED` | ❌ | Set `false` to disable emoji reactions (default: true) |
| `ZULIP_REACTION_CLEAR_ON_FINISH` | ❌ | Set `false` to keep start reactions on success (default: true) |
| `ZULIP_CHATMODE` | ❌ | Stream trigger mode: `onmessage` / `oncall` / `onchar` (default: onmessage) |
| `ZULIP_ONCHAR_PREFIXES` | ❌ | Custom onchar triggers, e.g. `!,>` (default: `!,>`, `@bot`) |
| `ZULIP_REQUIRE_MENTION` | ❌ | Set `false` to allow stream messages without mention (default: true) |
| `ZULIP_CHUNK_LIMIT` | ❌ | Max characters per message chunk (default: 4000) |
| `ZULIP_CHUNK_MODE` | ❌ | Chunking strategy: `length` or `newline` (default: length) |

## Plugin Version & Updates

| Problem | Solution |
|---------|----------|
| "Can't instantiate abstract class" | Add `async def get_chat_info()` to adapter |
| "No adapter available for zulip" | Check logs for missing SDK or syntax error |
| Bot not responding | Verify bot is subscribed to stream; check `ZULIP_ALLOWED_USERS` |
| Setup wizard shows instructions only | Ensure `setup_fn=interactive_setup` passed to `register()` |

For detailed agent instructions, see [AGENTS.md](AGENTS.md).

## Plugin Version & Updates

The plugin exposes its version so you can verify what is running.

### Checking the Version

In Zulip, DM the bot:
```
!version
```

The bot will reply with its current version, e.g.:
```
📬 Zulip plugin v1.5.0
You are on the latest version.
```

### Updating the Plugin

**Recommended:** Use the Git clone install (Option A above). Then updating is:

```bash
cd ~/.hermes/plugins/zulip
git pull origin main
hermes gateway restart
```

**If you used manual copy (Option B):**
```bash
# Remove old files
rm ~/.hermes/plugins/zulip/*.py ~/.hermes/plugins/zulip/plugin.yaml

# Re-copy from latest download
cp zulip/*.py ~/.hermes/plugins/zulip/
cp zulip/plugin.yaml ~/.hermes/plugins/zulip/

hermes gateway restart
```

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-change`
3. Make your changes
4. Test on a Hermes gateway: `python3 -m py_compile zulip/adapter.py`
5. Submit a pull request

## See Also

- [Hermes Plugin Docs](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters)
- [Zulip API Documentation](https://zulip.com/api/)

## License

MIT License — see [LICENSE](LICENSE) file.
