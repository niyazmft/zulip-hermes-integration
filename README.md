# 📬 Zulip Plugin for Hermes

[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-256%20passing-brightgreen)](https://github.com/niyazmft/zulip-hermes-integration/actions)
[![Latest Release](https://img.shields.io/github/v/release/niyazmft/zulip-hermes-integration?label=release)](https://github.com/niyazmft/zulip-hermes-integration/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Connect your Hermes AI agent to Zulip.** Chat with Hermes via **streams** (with automatic topic threading) or **DMs**. Supports admin commands, secure DM policies, file uploads, and health monitoring.

> 💡 **What this does:** Your Zulip bot becomes a doorway to your Hermes AI. Users type in Zulip, the AI thinks, the bot replies — all while keeping conversations threaded by topic.

---

## 🚀 Quickstart — Running in 2 Minutes

### 1. Install the Zulip SDK (one-time)

```bash
pip install "zulip>=0.9.0"
```

> ⚠️ Hermes doesn't auto-install plugin dependencies. Run this once in the same Python environment as Hermes.

### 2. Install the Plugin

```bash
mkdir -p ~/.hermes/plugins
rm -rf ~/.hermes/plugins/zulip
git clone https://github.com/niyazmft/zulip-hermes-integration.git ~/.hermes/plugins/zulip
hermes plugins enable zulip
```

### 3. Configure

Add to `~/.hermes/.env`:

```bash
ZULIP_API_KEY=your-bot-api-key
ZULIP_EMAIL=your-bot@niyaz.zulipchat.com
ZULIP_SITE=https://niyaz.zulipchat.com
```

Then add to `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    zulip:
      enabled: true
```

### 4. Start

```bash
hermes gateway
```

Send a DM or @-mention your bot in a subscribed stream. Done! 🎉

**For detailed setup**, see [docs/SETUP.md](docs/SETUP.md).  
**For admin configuration**, see the [Environment Variables](#environment-variables) section below.

---

## ✨ What You Get

### For End Users

| Feature | What it does |
|---------|-------------|
| 💬 **Streams + DMs** | Talk to the bot in public streams (with topic threading) or private messages |
| 🤔 **"Thinking..." placeholder** | Bot shows it's working, then edits with the final answer. No awkward silence. |
| 📎 **File uploads** | Send CSVs, PDFs, JSON — the bot downloads and can process them |
| 🏓 **Admin commands** | Type `/help`, `/status`, `/model` for instant responses (no LLM call needed) |

### For Admins

| Feature | What it does |
|---------|-------------|
| 🔐 **DM Policies** | Control who can DM: `open`, `allowlist`, `pairing` (code-based onboarding), or `disabled` |
| 🩺 **Health probe** | Pre-flight server check with SSRF protection + structured `health_status` logging |
| 🛡️ **Security hardening** | SSRF validation, symlink rejection, path traversal blocking |
| ⚡ **Performance caching** | LRU client + target caches reduce allocations and speed up sends |
| 📊 **Context metadata** | Every message carries `conversation_turn`, `session_gap_seconds`, `topic_changed` to help the AI avoid stale responses |
| 🔄 **One-command updates** | `bash ~/.hermes/plugins/zulip/update.sh` pulls latest and restarts |

### For Developers

| Feature | What it does |
|---------|-------------|
| 🔌 **Pure plugin** | Zero changes to Hermes core. Drop in, enable, done. |
| 🧩 **Extensible commands** | Add custom bot commands with `@register_command` decorator |
| 📁 **Sandboxed workspace** | Bot can generate files (reports, JSON, CSV) in a temp workspace with auto-cleanup |
| 🧪 **CI-tested** | 256 tests, pre-push hooks, GitHub Actions branch protection |

---

## 🏓 Built-in Commands

Type these in any stream or DM. They're handled instantly — no LLM call:

| Command | Response |
|---------|----------|
| `/help` | List all available commands |
| `/status` | Bot version, repo URL, your email |
| `/model` | Current model status |

Add your own:

```python
from zulip.commands import register_command

@register_command("ping")
def _cmd_ping(args, chat_id, sender_email, sender_name):
    return "🏓 Pong!"
```

---

## 🔐 DM Access Control

Set `ZULIP_DM_POLICY` to control who can message the bot:

| Mode | Behavior | Use case |
|------|----------|----------|
| `open` *(default)* | Anyone can DM | Small teams, public bots |
| `allowlist` | Only `ZULIP_ALLOWED_USERS` can DM | Internal team bots |
| `pairing` | New users get a pairing code to share with an admin | Moderated onboarding |
| `disabled` | All DMs blocked | Stream-only bots |

**Pairing mode flow:**

```
New user DM → "Your pairing code: PAIR-ABC123"
Admin approves → user can DM normally
```

---

## 📎 Sending Files

The bot can generate and send files as Zulip uploads:

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

Files appear as clickable links. Temp files auto-delete after upload. Path traversal and symlinks are rejected.

---

## 🏗️ Architecture

```
Zulip Stream/DM
    ↓
ZulipAdapter._listen_for_events()   # Event queue long-polling
    ↓
MessageEvent (with topic metadata + context fields)
    ↓
Gateway session → AI Agent
    ↓
ZulipAdapter.send() → Zulip REST API
```

All synchronous SDK calls are wrapped with `asyncio.to_thread()` to keep the gateway event loop responsive.

---

## 🔧 Environment Variables

### Required

| Variable | Example | Description |
|----------|---------|-------------|
| `ZULIP_API_KEY` | `abcd1234...` | Bot API key from Zulip settings |
| `ZULIP_EMAIL` | `bot@company.zulipchat.com` | Bot email address |
| `ZULIP_SITE` | `https://company.zulipchat.com` | Your Zulip organization URL |

### Optional — Access Control

| Variable | Default | Description |
|----------|---------|-------------|
| `ZULIP_ALLOWED_USERS` | *(empty)* | Comma-separated emails allowed to DM |
| `ZULIP_DM_POLICY` | `open` | `open` / `allowlist` / `pairing` / `disabled` |

### Optional — Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `ZULIP_CHATMODE` | `onmessage` | Stream trigger: `onmessage` / `oncall` / `onchar` |
| `ZULIP_REQUIRE_MENTION` | `true` | Stream messages need @mention (except `onmessage`) |
| `ZULIP_EDIT_PLACEHOLDER` | `true` | Show "Thinking..." placeholder while AI generates |
| `ZULIP_REACTIONS_ENABLED` | `true` | Emoji reactions (👀/✅/⚠️) for status |
| `ZULIP_CHUNK_LIMIT` | `4000` | Max chars per message chunk |

### Optional — Advanced

| Variable | Default | Description |
|----------|---------|-------------|
| `ZULIP_CHUNK_MODE` | `length` | Chunking strategy: `length` or `newline` |
| `ZULIP_ONCHAR_PREFIXES` | `!,>` | Custom onchar triggers |
| `ZULIP_BLOCK_STREAMING` | `false` | Experimental block streaming |
| `ZULIP_MEDIA_MAX_MB` | `5` | Max inbound attachment size (MB) |
| `ZULIP_ALLOW_ALL_USERS` | `false` | Disable all authorization (dev only) |

---

## 🆘 Troubleshooting

| Problem | Fix |
|---------|-----|
| "zulip package not installed" | Run `pip install "zulip>=0.9.0"` in Hermes's Python env |
| "No adapter available for zulip" | Check logs for syntax errors; verify `plugin.yaml` is present |
| Bot not responding in streams | Bot must be **subscribed** to the stream in Zulip settings |
| "Invalid or unsafe ZULIP_SITE" | Use `https://` URL, not `localhost` or IP addresses |
| Setup wizard shows instructions only | Ensure `setup_fn=interactive_setup` is passed to `register()` |

For detailed agent instructions, see [AGENTS.md](AGENTS.md).

---

## 🔄 Updating

```bash
# One-command update (downloads latest + restarts Hermes)
ssh user@device "bash ~/.hermes/plugins/zulip/update.sh"
```

Or manually:

```bash
cd ~/.hermes/plugins/zulip
git pull origin main
hermes gateway restart
```

---

## 🤝 Contributing

```bash
# 1. Fork and clone
git clone https://github.com/YOU/zulip-hermes-integration.git
cd zulip-hermes-integration

# 2. Install hooks
bash scripts/setup-hooks.sh

# 3. Make changes
# ...

# 4. Run checks
bash .githooks/pre-push

# 5. Submit PR (squash merge, branch protection enforced)
```

- **256 tests** — run via `pytest tests/`
- **Pre-push hook** — runs syntax checks + tests before every push
- **CI** — GitHub Actions `zulip-bridge` job must pass before merge
- **Branch protection** — requires PR + linear history + squash merge

---

## 📚 See Also

- [Hermes Plugin Docs](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters)
- [Zulip API Documentation](https://zulip.com/api/)
- [CHANGELOG.md](CHANGELOG.md)

## License

MIT License — see [LICENSE](LICENSE).
