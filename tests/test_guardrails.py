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
    for platform in (None, "frr", "arista_eos", "nokia_srlinux", "linux"):
        if platform == "linux" and command == "ip addr":
            continue
        with pytest.raises(CommandRejected):
            validate_read_command(command, platform)


@pytest.mark.parametrize(
    "command",
    [
        "info flat",
        "info",
        "info flat interface ethernet-1/1",
        "info from state network-instance default protocols bgp",
        "show network-instance default route-table ipv4-unicast summary",
        "show network-instance default protocols bgp neighbor",
        "show interface ethernet-1/1",
    ],
)
def test_accepts_srl_read_commands(command):
    validate_read_command(command, "nokia_srlinux")


@pytest.mark.parametrize("command", ["info flat", "info"])
def test_info_only_on_srl(command):
    for platform in (None, "frr", "arista_eos", "linux"):
        with pytest.raises(CommandRejected):
            validate_read_command(command, platform)


@pytest.mark.parametrize(
    "command",
    [
        "enter candidate",
        "enter candidate private",
        "info from candidate",
        "commit now",
        "info flat commit now",
        "discard now",
        "set / interface ethernet-1/1 admin-state disable",
        "show set",
        "load startup",
        "save startup",
        "tools system reboot",
        "file cat /etc/passwd",
        "info flat checkpoint",
        "rollback",
        "info delete",
        "info flat | grep x",
        "bash",
        "ip addr",
        "ping 10.0.0.1 network-instance default",
    ],
)
def test_rejects_srl_state_changes(command):
    with pytest.raises(CommandRejected):
        validate_read_command(command, "nokia_srlinux")


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


@pytest.mark.parametrize("command", ["show ip addr", "show ip route", "show interface", "show"])
def test_linux_hosts_have_no_show(command):
    with pytest.raises(CommandRejected, match="linux hosts have no 'show' commands; use "
                       "'ip addr' or 'ip route'"):
        validate_read_command(command, "linux")
    validate_read_command("show ip route", "frr")  # routers still accept show


@pytest.mark.parametrize("command", ["commit now", "commit stay", "enter candidate",
                                     "enter candidate private", "discard now", "quit",
                                     "exit all", "save startup", "Commit now"])
def test_srl_change_rejects_session_control(command):
    commands = ["set / interface ethernet-1/1 admin-state enable", command]
    with pytest.raises(CommandRejected, match="NetMedic enters candidate mode and commits"):
        validate_change_commands(commands, "nokia_srlinux")


def test_srl_change_accepts_set_lines():
    validate_change_commands(["set / interface ethernet-1/1 admin-state enable"],
                             "nokia_srlinux")


@pytest.mark.parametrize("platform", ["frr", "arista_eos"])
@pytest.mark.parametrize("command", ["configure terminal", "conf t", "configure session x",
                                     "end", "write memory", "wr", "copy running-config "
                                     "startup-config", "do write"])
def test_cli_change_rejects_session_control(platform, command):
    with pytest.raises(CommandRejected, match="enters and leaves configuration mode itself"):
        validate_change_commands([command, "interface eth2", "no shutdown"], platform)


@pytest.mark.parametrize("platform", ["frr", "arista_eos"])
def test_cli_change_keeps_exit_and_normal_lines(platform):
    validate_change_commands(["interface eth2", "no shutdown", "exit", "router ospf",
                              "network 10.0.12.0/30 area 0", "description endpoint"],
                             platform)


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
