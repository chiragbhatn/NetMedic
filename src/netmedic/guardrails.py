"""Command validation and output limits (SPEC section 10)."""

from __future__ import annotations

import ipaddress
import re

from netmedic.errors import CommandRejected
from netmedic.platforms import PLATFORMS

MAX_OUTPUT_CHARS = 8000
MAX_COMMAND_CHARS = 200  # no legitimate read-only command comes close to this
TRUNCATION_MARKER = "\n[truncated: output exceeded {limit} characters]"

FORBIDDEN_CHARS = ("|", ";", "&", ">", "<", "`", "$(", "\n", "\r")
FORBIDDEN_WORDS = (
    "configure", "reload", "write", "copy", "delete", "erase", "clear", "shutdown", "bash",
)
LINUX_READ_PREFIXES = PLATFORMS["linux"].read_prefixes
# SR Linux words that enter candidate mode, commit, or otherwise change state or files.
SRL_FORBIDDEN = re.compile(
    r"\b(enter|candidate|commit|discard|rollback|load|save|set|replace|insert|tools|"
    r"file|checkpoint|request|baseline|private|exclusive|annotate)\b"
)
CHANGE_FORBIDDEN = re.compile(r"\b(reload|erase|delete|format)\b", re.IGNORECASE)
# Session control in a proposed change. The driver enters config mode itself (and on SR
# Linux enters candidate mode and commits), so these lines would be wrong or run twice.
SRL_SESSION = re.compile(
    r"^(commit|enter|discard|quit|exit|save|rollback|load|baseline|validate|tools)\b",
    re.IGNORECASE,
)
CLI_SESSION = re.compile(r"^(conf(igure|igur|igu|ig|i)?|end|write|wr|copy|do)\b",
                         re.IGNORECASE)
LINUX_NO_SHOW = ("linux hosts have no 'show' commands; use 'ip addr' or 'ip route' "
                 "(or the get_interfaces / get_routes tools)")


def validate_read_command(command: str, platform: str | None = None) -> None:
    """Raise CommandRejected unless `command` is a safe read-only command.

    `show ...` is accepted on routers (not on linux hosts, which have no `show`). A
    platform may allow more read-only prefixes (netmedic.platforms): `ip addr` /
    `ip route` on linux, `info ...` on nokia_srlinux.
    """
    if len(command) > MAX_COMMAND_CHARS:
        raise CommandRejected(
            f"command is too long ({len(command)} characters; the limit is {MAX_COMMAND_CHARS})"
        )
    for token in FORBIDDEN_CHARS:
        if token in command:
            raise CommandRejected(f"command contains forbidden characters {token!r}")
    lowered = command.strip().lower()
    for word in FORBIDDEN_WORDS:
        if word in lowered:
            raise CommandRejected(f"command contains forbidden keyword '{word}'")
    if platform == "nokia_srlinux":
        match = SRL_FORBIDDEN.search(lowered)
        if match:
            raise CommandRejected(f"command contains forbidden keyword '{match.group(1)}'")
    if lowered == "show" or lowered.startswith("show "):
        if platform == "linux":
            raise CommandRejected(LINUX_NO_SHOW)
        return
    extra = PLATFORMS[platform].read_prefixes if platform in PLATFORMS else ()
    if any(lowered == p or lowered.startswith(p + " ") for p in extra):
        return
    raise CommandRejected(
        "only 'show ...' commands are allowed on routers "
        "('ip addr'/'ip route' on linux, also 'info ...' on nokia_srlinux)"
    )


def validate_change_commands(commands: list[str], platform: str | None = None) -> None:
    """Raise CommandRejected if a proposed config change contains a destructive command,
    or a line that enters, leaves, commits or saves the configuration session."""
    if not commands:
        raise CommandRejected("a change needs at least one command")
    for command in commands:
        if any(c in command for c in ("\n", "\r")):
            raise CommandRejected("change commands must not contain newlines")
        match = CHANGE_FORBIDDEN.search(command)
        if match:
            raise CommandRejected(f"change contains forbidden command '{match.group(1)}'")
        if platform == "nokia_srlinux" and SRL_SESSION.match(command.strip()):
            raise CommandRejected(
                f"'{command}' is not a configuration line: NetMedic enters candidate mode "
                "and commits the change itself; send only 'set ...' configuration lines, "
                "e.g. 'set / interface ethernet-1/1 admin-state enable'"
            )
        if platform in ("frr", "arista_eos") and CLI_SESSION.match(command.strip()):
            raise CommandRejected(
                f"'{command}' is not a configuration line: NetMedic enters and leaves "
                "configuration mode itself and does not save the config; send only the "
                "configuration lines, e.g. ['interface eth2', 'no shutdown']"
            )


def validate_ipv4(target: str) -> None:
    try:
        ipaddress.IPv4Address(target)
    except ValueError:
        raise CommandRejected("target must be an IPv4 address") from None


def truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + TRUNCATION_MARKER.format(limit=MAX_OUTPUT_CHARS)
