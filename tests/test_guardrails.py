import pytest

from netmedic.errors import CommandRejected
from netmedic.guardrails import (
    MAX_OUTPUT_CHARS,
    truncate,
    validate_change_commands,
    validate_ipv4,
    validate_read_command,
)


@pytest.mark.parametrize(
    "command",
    [
        "show ip route",
        "show interface brief",
        "show ip interface brief",
        "show interfaces Ethernet1",
        "show ip route 192.168.3.0/24",
        "show ip ospf neighbor",
        "show ip bgp summary",
        "show running-config",
        "show ip access-lists",
    ],
)
def test_accepts_show_commands(command):
    validate_read_command(command)


@pytest.mark.parametrize("command", ["ip addr", "ip route"])
def test_accepts_ip_commands_on_linux_only(command):
    validate_read_command(command, "linux")
    with pytest.raises(CommandRejected):
        validate_read_command(command, "frr")


@pytest.mark.parametrize(
    "command",
    [
        "show ip route | include 10",
        "show ip route; reload",
        "show ip route & reload",
        "show ip route > /tmp/x",
        "show ip route < /tmp/x",
        "show `id`",
        "show $(id)",
        "show ip route\nreload",
        "show configure",
        "show reload",
        "show write",
        "show copy",
        "show delete",
        "show erase",
        "show clear",
        "show shutdown",
        "show bash",
        "configure terminal",
        "ping 10.0.0.1",
        "ip addr",
        "",
    ],
)
def test_rejects_unsafe_commands(command):
    with pytest.raises(CommandRejected):
        validate_read_command(command)


@pytest.mark.parametrize(
    "commands",
    [
        ["reload"],
        ["write erase"],
        ["delete flash:startup"],
        ["format flash:"],
        ["erase startup"],
        [],
    ],
)
def test_rejects_destructive_changes(commands):
    with pytest.raises(CommandRejected):
        validate_change_commands(commands)


def test_accepts_normal_change():
    validate_change_commands(["interface eth2", "no shutdown"])


@pytest.mark.parametrize("target", ["10.0.0.1", "192.168.3.10"])
def test_ipv4_ok(target):
    validate_ipv4(target)


@pytest.mark.parametrize("target", ["host2", "10.0.0.256", "10.0.0.1; reboot", "::1", ""])
def test_ipv4_rejected(target):
    with pytest.raises(CommandRejected, match="IPv4"):
        validate_ipv4(target)


def test_truncate():
    assert truncate("short") == "short"
    exact = "x" * MAX_OUTPUT_CHARS
    assert truncate(exact) == exact
    long = truncate("y" * (MAX_OUTPUT_CHARS + 50))
    assert long.startswith("y" * MAX_OUTPUT_CHARS)
    assert "[truncated" in long
    assert long.count("y") == MAX_OUTPUT_CHARS
