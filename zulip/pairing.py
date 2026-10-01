"""Approve Zulip DM pairing requests from the command line (issue #198).

``ZULIP_DM_POLICY=pairing`` hands a new user a code and asks them to contact an
admin — but the plugin has no admin concept, and no in-chat command can be
exposed safely without one (the gateway already owns ``/approve`` for exec
approval, and a name it owns must not be shadowed). So approval happens here,
where the gateway's data already lives: anyone who can run this command
already holds the host access the approval implies.

    python3 -m zulip.pairing list
    python3 -m zulip.pairing approve PAIR-OHJ4OI
    python3 -m zulip.pairing approve newbie@example.com
    python3 -m zulip.pairing revoke newbie@example.com

Pending requests are persisted in the same file as the allowlist, so this sees
them even though the gateway is a different process. The running gateway picks
the change up on the next DM it handles (one ``stat()`` on that file), so a
restart is not required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import runtime_scope
from .policy import PolicyEngine

__all__ = ["main"]


def _format_age(seconds: float) -> str:
    """Render an age as a short human string (``45s``, ``12m``, ``3h``)."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


def _build_engine(data_dir: str | None) -> PolicyEngine:
    """Build a PolicyEngine against the gateway's data directory."""
    resolved = data_dir or runtime_scope.get_profile_data_dir()
    return PolicyEngine(data_dir=resolved)


def _cmd_list(engine: PolicyEngine) -> int:
    pending = engine.list_pending()
    if not pending:
        print("No pending pairing requests.")
        return 0
    print(f"{len(pending)} pending pairing request(s):")
    print()
    for code, email, age in pending:
        print(f"  PAIR-{code}  {email}  ({_format_age(age)} old)")
    print()
    print("Approve one with:  python3 -m zulip.pairing approve PAIR-<code>")
    return 0


def _cmd_approve(engine: PolicyEngine, target: str) -> int:
    target = target.strip()
    # A code has no "@"; anything else is an email. Codes are accepted with or
    # without the PAIR- prefix, in any case.
    if "@" in target:
        email = target.lower()
        engine.approve_email(email)
        print(f"Approved {email}.")
        return 0

    email = engine.approve_code(target)
    if email is None:
        print(
            f"No pending request for code {target!r}.\n"
            "It may have expired (codes last 24 hours), already been used, or\n"
            "never existed. Run `python3 -m zulip.pairing list` to see what is pending.",
            file=sys.stderr,
        )
        return 1
    print(f"Approved {email}.")
    return 0


def _cmd_revoke(engine: PolicyEngine, email: str) -> int:
    email = email.strip().lower()
    if engine.revoke_email(email):
        print(f"Revoked {email}.")
        return 0
    print(f"{email} was not on the allowlist.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python3 -m zulip.pairing",
        description="Manage Zulip DM pairing requests and the DM allowlist.",
        epilog=(
            "The running gateway notices changes on its next DM, so no restart "
            "is needed."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Hermes data directory (default: the active profile's home)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show pending pairing requests")

    approve = sub.add_parser("approve", help="Approve a request by code or email")
    approve.add_argument("code_or_email", help="PAIR-XXXXXX, XXXXXX, or an email")

    revoke = sub.add_parser("revoke", help="Remove an email from the allowlist")
    revoke.add_argument("email")

    args = parser.parse_args(argv)
    engine = _build_engine(args.data_dir)

    if args.command == "list":
        return _cmd_list(engine)
    if args.command == "approve":
        return _cmd_approve(engine, args.code_or_email)
    if args.command == "revoke":
        return _cmd_revoke(engine, args.email)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
