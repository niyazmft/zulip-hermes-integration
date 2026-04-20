# Zulip Integration Setup Guide

This guide walks you through setting up Zulip integration for Hermes Agent.

## Prerequisites

- Python 3.8+
- Zulip organization (self-hosted or zulipchat.com)
- Admin access to create bots in your Zulip organization
- Hermes Agent installed

## Step 1: Install Dependencies

```bash
cd zulip-hermes-integration
pip install -r requirements.txt
```

## Step 2: Create Zulip Bot

1. **Log in to Zulip** as an admin
2. **Go to Settings** (gear icon in top right)
3. **Navigate to "Bots"** in the left sidebar
4. **Click "Add a new bot"**
5. **Fill in the form:**
   - Bot type: **Generic bot**
   - Bot name: `Hermes Agent` (or your preferred name)
   - Bot email: auto-generated (e.g., `hermes-bot@niyaz.zulipchat.com`)
6. **Click "Create bot"**
7. **Copy the API key** shown (you won't see it again!)

## Step 3: Configure Environment

1. **Copy the example env file:**
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env` with your credentials:**
   ```bash
   ZULIP_API_KEY=<paste-your-api-key>
   ZULIP_EMAIL=hermes-bot@niyaz.zulipchat.com
   ZULIP_SITE=https://niyaz.zulipchat.com
   ZULIP_ALLOWED_USERS=your-email@domain.com
   ```

3. **Find your Stream ID (for cron deliveries):**
   - Go to the stream in Zulip
   - Click the stream name → "Stream settings"
   - The URL will show the stream ID: `#narrow/stream/573423-general`
   - The number (573423) is your stream ID

## Step 4: Test Connection

Run the test script:

```bash
python examples/basic_bot.py
```

You should see:
```
Zulip adapter connecting...
Zulip connection established
Zulip adapter listening for messages...
Bot is running. Press Ctrl+C to stop.
```

Send a message to the bot in Zulip. It should respond (once you've integrated it with Hermes Gateway).

## Step 5: Integrate with Hermes Gateway

### Option A: Manual Integration

Copy the adapter file to your Hermes installation:

```bash
cp src/zulip_adapter.py /path/to/hermes/gateway/platforms/
```

Update the following Hermes files:

1. **`gateway/config.py`** - Add Platform.ZULIP enum
2. **`gateway/run.py`** - Register Zulip adapter in factory
3. **`tools/send_message_tool.py`** - Add Zulip to platform map
4. **`cron/scheduler.py`** - Add Zulip to delivery platform map
5. **`toolsets.py`** - Create hermes-zulip toolset

### Option B: Use the Skill (Recommended)

If you're using Hermes with skills:

```bash
# The skill is already in your Hermes installation
# Just ensure the environment variables are set
```

The skill location is: `~/.hermes/skills/zulip-integration/`

## Step 6: Configure Authorization

### Restrict to Specific Users

```bash
ZULIP_ALLOWED_USERS=user1@domain.com,user2@domain.com
```

### Allow All Users (Development Only)

```bash
ZULIP_ALLOW_ALL_USERS=true
```

⚠️ **Warning:** Only use `ZULIP_ALLOW_ALL_USERS` in development. In production, always specify allowed users.

## Step 7: Set Up Cron Deliveries

See `examples/cron_delivery.py` for examples of:
- Daily project pulses
- Weekly security scans
- PR notifications
- One-time reports

## Step 8: Subscribe Bot to Streams

By default, the bot only sees messages where it's mentioned or in DMs.

To receive all messages in a stream:

1. **Go to stream settings**
2. **Click "Subscribers"**
3. **Add your bot** to the subscriber list

## Troubleshooting

### Bot Not Receiving Messages

- ✅ Check bot is subscribed to the stream
- ✅ Verify `ZULIP_ALLOWED_USERS` includes your email
- ✅ Ensure bot has permission to read the stream

### Connection Failed

- ✅ Verify `ZULIP_API_KEY` is correct
- ✅ Check `ZULIP_SITE` URL (no trailing slash)
- ✅ Test with the connection test script

### Cron Delivery Fails

- ✅ Stream ID must be numeric (string format in config)
- ✅ Bot must have send permission in the stream
- ✅ Topic is optional but recommended for organization

## Next Steps

- Set up automated notifications
- Configure RBAC policies
- Create custom integrations with other tools
- Monitor bot activity and logs

## Support

For issues or questions:
- Check the main README.md
- Review examples in `examples/` directory
- Consult Hermes Agent documentation
