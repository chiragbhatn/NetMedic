"""Per-platform logical commands: the single source of truth (SPEC sections 6 and 7).

Tools, the replay fixtures and `netmedic record` all key on these exact strings.
"""

from __future__ import annotations

from dataclasses import dataclass

from netmedic.errors import NetMedicError

SRL_NI = "network-instance default"


@dataclass(frozen=True)
class PlatformCommands:
    """Logical commands for one platform. `None` means the platform has no such command.

    Templates use `{name}` (interface), `{prefix}` (IPv4 prefix) and `{target}` (IPv4).
    """

    interfaces: str
    routes: str
    ping: str
    traceroute: str
    interface: str | None = None
    route: str | None = None
    ospf_neighbors: str | None = None
    ospf_interfaces: str | None = None
    bgp_neighbors: str | None = None
    running_config: str | None = None
    acls: str | None = None
    # Read-only command prefixes accepted besides "show " (see guardrails).
    read_prefixes: tuple[str, ...] = ()


PLATFORMS: dict[str, PlatformCommands] = {
    "frr": PlatformCommands(
        interfaces="show interface brief",
        interface="show interface {name}",
        routes="show ip route",
        route="show ip route {prefix}",
        ospf_neighbors="show ip ospf neighbor",
        ospf_interfaces="show ip ospf interface",
        bgp_neighbors="show ip bgp summary",
        running_config="show running-config",
        ping="ping {target}",
        traceroute="traceroute {target}",
    ),
    "arista_eos": PlatformCommands(
        interfaces="show ip interface brief",
        interface="show interfaces {name}",
        routes="show ip route",
        route="show ip route {prefix}",
        ospf_neighbors="show ip ospf neighbor",
        ospf_interfaces="show ip ospf interface",
        bgp_neighbors="show ip bgp summary",
        running_config="show running-config",
        acls="show ip access-lists",
        ping="ping {target}",
        traceroute="traceroute {target}",
    ),
    # Unverified on a live device: SR Linux 24.10 CLI syntax from documentation/memory.
    "nokia_srlinux": PlatformCommands(
        interfaces="show interface brief",
        interface="show interface {name}",
        routes=f"show {SRL_NI} route-table ipv4-unicast summary",
        route=f"show {SRL_NI} route-table ipv4-unicast prefix {{prefix}}",
        ospf_neighbors=f"show {SRL_NI} protocols ospf neighbor",
        ospf_interfaces=f"show {SRL_NI} protocols ospf interface",
        bgp_neighbors=f"show {SRL_NI} protocols bgp neighbor",
        running_config="info flat",
        ping=f"ping {{target}} {SRL_NI}",
        traceroute=f"traceroute {{target}} {SRL_NI}",
        read_prefixes=("info",),
    ),
    "linux": PlatformCommands(
        interfaces="ip addr",
        routes="ip route",
        ping="ping {target}",
        traceroute="traceroute {target}",
        read_prefixes=("ip addr", "ip route"),
    ),
}

ROUTER_PLATFORMS = ("frr", "arista_eos", "nokia_srlinux")

# --- what `netmedic record` captures (and what healthy fixtures must contain) ----------

RECORD_INTERFACES = {
    "r1": ["eth1", "eth2"],
    "r2": ["eth1", "eth2", "eth3"],
    "r3": ["Ethernet1", "Ethernet2"],
    "r4": ["ethernet-1/1", "ethernet-1/2"],
}
RECORD_PREFIXES = [
    "192.168.1.0/24", "192.168.3.0/24", "192.168.4.0/24",
    "10.255.0.1/32", "10.255.0.2/32", "10.255.0.3/32", "10.255.0.4/32",
]
PROBE_TARGETS = [
    "192.168.1.10", "192.168.3.10", "192.168.4.10",
    "10.255.0.1", "10.255.0.2", "10.255.0.3", "10.255.0.4",
]


def commands_for(platform: str) -> PlatformCommands:
    try:
        return PLATFORMS[platform]
    except KeyError:
        known = ", ".join(sorted(PLATFORMS))
        raise NetMedicError(f"unknown platform '{platform}' (known: {known})") from None


def probe_command(platform: str, tool: str, target: str) -> str:
    """The logical ping/traceroute command for a platform."""
    cmds = commands_for(platform)
    template = cmds.ping if tool == "ping" else cmds.traceroute
    return template.format(target=target)


def record_commands(name: str, platform: str) -> list[str]:
    """Every logical command recorded for one device (SPEC sections 6 and 7)."""
    cmds = commands_for(platform)
    if platform in ROUTER_PLATFORMS:
        commands = [cmds.interfaces, cmds.routes, cmds.ospf_neighbors, cmds.ospf_interfaces,
                    cmds.bgp_neighbors, cmds.running_config]
        if cmds.acls:
            commands.append(cmds.acls)
        commands += [cmds.interface.format(name=i) for i in RECORD_INTERFACES.get(name, [])]
        commands += [cmds.route.format(prefix=p) for p in RECORD_PREFIXES]
    else:
        commands = [cmds.interfaces, cmds.routes]
    commands += [probe_command(platform, tool, t)
                 for tool in ("ping", "traceroute") for t in PROBE_TARGETS]
    return [c for c in commands if c]
