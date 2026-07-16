# Zulip Hermes Plugin — Agent Instructions

## Project Type

Hermes Agent gateway adapter plugin. Adds Zulip as a first-class messaging platform alongside Telegram, Discord, Slack, etc. Lives as a drop-in plugin under `plugins/platforms/zulip/`.

## Toolchain & Commands

```bash
python3 -m py_compile zulip/adapter.py      # Syntax-check the adapter
python3 -m py_compile zulip/__init__.py     # Syntax-check the entry point

# Validate YAML
python3 -c "import yaml; yaml.safe_load(open('zulip/plugin.yaml'))"

# Lint (if available)
gdformat --check zulip/adapter.py 2>/dev/null || echo "gdformat not installed"
```

## Architecture

```
Hermes Gateway
    ↓  platform_registry.create_adapter("zulip")
ZulipAdapter(BasePlatformAdapter)
    ├── connect()          → Zulip event queue registration + async poll loop
    ├── disconnect()       → Cancel event task + mark disconnected
    ├── send()             → ZulipClient.send_message() (stream or DM)
    ├── get_chat_info()    → {"name": chat_id, "type": "dm" | "stream"}
    └── _listen_for_events() → Event queue long-poll → _handle_message()
```

### Key Files

| File | Purpose |
|------|---------|
| `zulip/adapter.py` | `ZulipAdapter` class + `register()` + `interactive_setup()` |
| `zulip/__init__.py` | Plugin entry point: `from .adapter import register` |
| `zulip/plugin.yaml` | Plugin metadata, `requires_env`/`optional_env` for `hermes config` UI |

### Message Flow

1. **Inbound**: Zulip event queue → `_handle_message()` → `self.handle_message(event)` → Gateway session → AIAgent
2. **Outbound**: AIAgent response → Gateway → `ZulipAdapter.send()` → Zulip REST API
3. **Threading**: `_topic_cache` stores last-seen topic per stream; replies preserve thread

### Chat ID Format

| Type | Format | Example |
|------|--------|---------|
| Stream | Numeric stream ID | `"573423"` |
| DM | `dm:` + user ID | `"dm:1032616"` |

Topic is passed via `metadata={"topic": "..."}` on send.

## Plugin Registration

The plugin is discovered by Hermes' `PluginManager._scan_directory()` which looks for:
- **Bundled**: `plugins/platforms/zulip/plugin.yaml` (dist-packages)
- **User**: `~/.hermes/plugins/zulip/plugin.yaml`

The `kind: platform` in `plugin.yaml` triggers lazy registration via `_register_deferred_platform()`. The actual module import (heavy `zulip` SDK) only happens when the gateway first needs the platform.

## Dependencies

| Package | Required | Where declared |
|---------|----------|----------------|
| `zulip` | ✅ Runtime | `requirements.txt` (not enforced by Hermes plugin loader) |

The plugin uses a lazy import guard in `adapter.py`:

```python
try:
    import zulip
    ZULIP_AVAILABLE = True
except ImportError:
    zulip = None
    ZULIP_AVAILABLE = False
```

`__init__.py` must NOT import `zulip` at module load time — the gateway defers platform module loading until first use, so a missing `zulip` package only fails when the adapter is instantiated, not at gateway startup.

**Container gotcha:** If Hermes runs inside Docker, `pip install zulip` inside a running container is ephemeral (overlay filesystem). Either:
1. Bake `zulip` into the image (`RUN pip install zulip` in Dockerfile)
2. Auto-install in `docker-entrypoint.sh` before starting Hermes
3. Mount a persistent volume for site-packages

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ZULIP_API_KEY` | ✅ | Bot API key |
| `ZULIP_EMAIL` | ✅ | Bot email address |
| `ZULIP_SITE` | ✅ | Organization URL |
| `ZULIP_ALLOWED_USERS` | ❌ | Comma-separated authorized emails |
| `ZULIP_ALLOW_ALL_USERS` | ❌ | `"true"` to skip authorization (dev only) |
| `ZULIP_HOME_CHANNEL` | ❌ | Default stream ID for cron |
| `ZULIP_HOME_CHANNEL_NAME` | ❌ | Default topic for cron |

## Configuration

Config is read from `~/.hermes/.env` (env vars) and `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    zulip:
      enabled: true
```

The `env_enablement_fn()` seeds `PlatformConfig.extra` from env vars before adapter construction.

## Async Patterns

All synchronous Zulip SDK calls must be wrapped in `asyncio.to_thread()`:

```python
result = await asyncio.to_thread(self.client.get_members)
```

The gateway event loop must not be blocked — Zulip SDK is synchronous-only.

## Authorization

Authorization is **handled by the plugin system** via `allowed_users_env`/`allow_all_env` passed to `ctx.register_platform()`. The adapter does NOT implement its own `_is_authorized()` — the gateway checks before calling `handle_message()`.

## Cron Delivery

- `cron_deliver_env_var="ZULIP_HOME_CHANNEL"` makes `deliver=zulip:stream_id` work
- `standalone_sender_fn` handles out-of-process sends (when `hermes cron run` runs separately from `hermes gateway`)

## Testing

### Manual Test Flow

1. Install plugin files to bundled or user path
2. `hermes plugins enable zulip`
3. `hermes gateway setup` → select 📬 Zulip → enter credentials
4. `hermes gateway`
5. Send DM or stream message in Zulip
6. Check logs: `tail -f ~/.hermes/logs/gateway.log | grep -i zulip`

### Expected Log Output (Success)

```
hermes_plugins.zulip.adapter: Zulip adapter connecting...
hermes_plugins.zulip.adapter: Zulip connection established
gateway.run: ✓ zulip connected
gateway.run: inbound message: platform=zulip user=... chat=dm:... msg=...
gateway.run: response ready: platform=zulip ...
gateway.platforms.base: [Zulip] Sending response (...) to dm:...
```

## Known Gotchas

### "Can't instantiate abstract class without get_chat_info"
`get_chat_info()` is `@abstractmethod` in `BasePlatformAdapter`. Must implement it or adapter creation fails at gateway startup.

### "No adapter available for zulip"
Adapter creation failed (check logs for the real error — usually missing abstract method or `zulip` SDK not installed).

### Messages not received (no log activity)
- Bot not subscribed to stream in Zulip UI
- Message sent to wrong stream / topic
- Authorization failed (check `ZULIP_ALLOWED_USERS`)
- `handle_message()` exception — check logs for tracebacks

### "setup wizard shows instructions instead of prompts"
The `register()` call must pass `setup_fn=interactive_setup`. Without it, the gateway falls back to generic env-var instructions.

### `connect()` signature mismatch
Must be `async def connect(self, *, is_reconnect: bool = False) -> bool:`. The `is_reconnect` kwarg was added in a recent Hermes version.

### Hermes home directory confusion
The container uses `HOME=/paperclip`, so `~/.hermes/.env` resolves to `/paperclip/.hermes/.env`, not `/root/.hermes/.env`. The gateway reads env vars from the Hermes home, not the shell's `$HOME`.

## Hermes Version Compatibility

- Tested on **v0.18.2** (2026.7.7.2)
- Plugin system uses deferred loading for `kind: platform`
- `BasePlatformAdapter` abstract methods: `connect()`, `disconnect()`, `send()`, `get_chat_info()`

## License

MIT — see LICENSE file.
