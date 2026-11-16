"""Command validation and output limits (SPEC section 10)."""

from __future__ import annotations

import ipaddress
import re

from netmedic.errors import CommandRejected

MAX_OUTPUT_CHARS = 8000
TRUNCATION_MARKER = "\n[truncated: output exceeded {limit} characters]"

FORBIDDEN_CHARS = ("|", ";", "&", ">", "<", "`", "$(", "\n", "\r")
FORBIDDEN_WORDS = (
    "configure", "reload", "write", "copy", "delete", "erase", "clear", "shutdown", "bash",
)
LINUX_READ_PREFIXES = ("ip addr", "ip route")
CHANGE_FORBIDDEN = re.compile(r"\b(reload|erase|delete|format)\b", re.IGNORECASE)


def validate_read_command(command: str, platform: str | None = None) -> None:
    """Raise CommandRejected unless `command` is a safe read-only command.

    `show ...` is accepted everywhere; `ip addr` / `ip route` only when platform is linux.
    """
    for token in FORBIDDEN_CHARS:
        if token in command:
            raise CommandRejected(f"command contains forbidden characters {token!r}")
    lowered = command.strip().lower()
    for word in FORBIDDEN_WORDS:
        if word in lowered:
            raise CommandRejected(f"command contains forbidden keyword '{word}'")
    if lowered.startswith("show "):
        return
    if platform == "linux" and any(
        lowered == p or lowered.startswith(p + " ") for p in LINUX_READ_PREFIXES
    ):
        return
    raise CommandRejected("only 'show ...' commands are allowed (or 'ip addr'/'ip route' on linux)")


def validate_change_commands(commands: list[str]) -> None:
    """Raise CommandRejected if a proposed config change contains a destructive command."""
    if not commands:
        raise CommandRejected("a change needs at least one command")
    for command in commands:
        if any(c in command for c in ("\n", "\r")):
            raise CommandRejected("change commands must not contain newlines")
        match = CHANGE_FORBIDDEN.search(command)
        if match:
            raise CommandRejected(f"change contains forbidden command '{match.group(1)}'")


def validate_ipv4(target: str) -> None:
    try:
        ipaddress.IPv4Address(target)
    except ValueError:
        raise CommandRejected("target must be an IPv4 address") from None


def truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + TRUNCATION_MARKER.format(limit=MAX_OUTPUT_CHARS)
