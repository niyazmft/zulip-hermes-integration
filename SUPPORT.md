# Support

## Getting Help

- **Documentation**: [README.md](README.md) for install, configuration and the full env-var
  reference; [AGENTS.md](AGENTS.md) for how the bot behaves in a room
- **Issues**: search [existing issues](https://github.com/niyazmft/zulip-hermes-integration/issues)
  before filing a new one
- **Security**: [SECURITY.md](SECURITY.md) — threat model and how to report a vulnerability
  privately. Do not open a public issue for a security problem.

## Troubleshooting

Most "the bot is not responding" reports are one of four things, and the README's
[Troubleshooting](README.md#troubleshooting) and AGENTS.md's tables name the config behind
each:

- the bot is not subscribed to the stream, or was never mentioned in it
- `ZULIP_CHATMODE` / `ZULIP_GROUP_POLICY` / `ZULIP_GROUP_ALLOW_FROM` do not allow the sender
- the message was queued behind a running turn (the bot works one request at a time per topic)
- a sticky-engagement window lapsed, so a follow-up with no mention is no longer addressed to it

Logs are the fastest way in: gateway logs land in the Hermes log directory, and the plugin's
own audit events are listed in [SECURITY.md](SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and the checks CI will run, and
[docs/RELEASING.md](docs/RELEASING.md) for the release procedure.
