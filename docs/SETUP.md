# Zulip Plugin Setup Guide

## Prerequisites

- Python 3.8+
- Hermes Agent installed
- Zulip organization (self-hosted or zulipchat.com)
- Admin access to create bots in your Zulip organization

## Step 1: Create a Zulip Bot

1. Log in to Zulip as an admin
2. Go to **Settings → Bots** in the left sidebar
3. Click **Add a new bot**
4. Choose **Generic bot**
5. Copy the API key and note the bot's email address

## Step 2: Install the Plugin

### Option A: User plugin (recommended for most installs)

```bash
mkdir -p ~/.hermes/plugins/zulip
cp zulip/__init__.py zulip/adapter.py zulip/plugin.yaml ~/.hermes/plugins/zulip/
hermes plugins enable zulip
```

### Option B: Bundled plugin (for containers / system-wide installs)

```bash
HERMES_PATH=$(python3 -c "import hermes_cli; print(hermes_cli.__path__[0])")
mkdir -p "$HERMES_PATH/../plugins/platforms/zulip"
cp zulip/__init__.py zulip/adapter.py zulip/plugin.yaml "$HERMES_PATH/../plugins/platforms/zulip/"
```

## Step 3: Interactive Onboarding

Run the setup wizard. No manual `.env` editing required — it reads the plugin's `requires_env`/`optional_env` declarations and prompts interactively:

```bash
hermes gateway setup
```

Follow the prompts for:
- **Zulip API key** (password-masked)
- **Bot email**
- **Site URL**
- **Allowed users** (optional)
- **Home channel** & **topic** (optional)

The wizard saves everything to `~/.hermes/.env` and offers to start the gateway.

### Updating credentials later

```bash
hermes config
```

## Step 4: Subscribe Bot to Streams

By default, the bot only sees DMs and @-mentions.
To receive all messages in a stream:

1. Go to **Stream settings → Subscribers**
2. Add your bot

## Step 5: Start Gateway

If the wizard didn't already start it:

```bash
hermes gateway
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot not responding | Check `ZULIP_ALLOWED_USERS` includes your email; verify bot is subscribed to the stream |
| Cron delivery fails | Verify `ZULIP_HOME_CHANNEL` is a numeric stream ID |
| Connection errors | Verify API key, email, and site URL (no trailing slash) |
| "zulip package not installed" | Run `pip install zulip` |

## See Also

- [Hermes Plugin Docs](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters)
- [Zulip API Documentation](https://zulip.com/api/)
