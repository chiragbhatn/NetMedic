"""The Toolbox: every device tool, behind guardrails and the audit log (SPEC sections 9, 10)."""

from __future__ import annotations

import difflib
import ipaddress
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from netmedic.audit import AuditLog
from netmedic.drivers.base import Driver
from netmedic.errors import ApprovalPending, NetMedicError
from netmedic.guardrails import (
    truncate,
    validate_change_commands,
    validate_ipv4,
    validate_read_command,
)
from netmedic.inventory import Device, Inventory
from netmedic.platforms import ROUTER_PLATFORMS, PlatformCommands, commands_for, probe_command

CHANGE_ID_RE = re.compile(r"^[a-f0-9]{1,32}$")
INTERFACE_RE = re.compile(r"^[A-Za-z0-9/._:-]+$")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema object
    is_change: bool


@dataclass
class ChangeProposal:
    id: str
    device: str
    commands: list[str]
    reason: str
    status: str  # proposed | applied | rolled_back | denied
    rollback_config: str | None


Approver = Callable[[ChangeProposal], bool]


class ToolError(NetMedicError):
    """A tool-level failure reported to the caller as `ERROR: <reason>`."""


RUNNING_CONFIG_PREAMBLE = ("Building configuration...", "Current configuration:")


def clean_running_config(text: str) -> str:
    """Drop the FRR `show running-config` preamble and leading blank lines.

    EOS `! Command:` / `! device:` lines are comments and are kept.
    """
    lines = text.splitlines()
    while lines and (not lines[0].strip() or lines[0].strip() in RUNNING_CONFIG_PREAMBLE):
        lines.pop(0)
    return "\n".join(lines) + "\n" if lines else ""


def _schema(properties: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


_DEVICE = {"type": "string", "description": "Inventory device name, e.g. r1 or host1."}
_TARGET = {"type": "string", "description": "IPv4 address to probe, e.g. 192.168.3.10."}
_CHANGE_ID = {"type": "string", "description": "Id returned by propose_change."}

SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "list_devices",
        "List inventory devices with their platform and role.",
        _schema(),
        False,
    ),
    ToolSpec(
        "run_show",
        "Run a read-only command on a device. Routers accept 'show ...' commands "
        "(nokia_srlinux also 'info ...'); linux hosts have no 'show' and accept only "
        "'ip addr' / 'ip route'. "
        "Returns raw CLI text.",
        _schema(
            {"device": _DEVICE, "command": {"type": "string", "description": "e.g. show ip route"}},
            ("device", "command"),
        ),
        False,
    ),
    ToolSpec(
        "get_interfaces",
        "Interface status for a device, or details of one interface on a router.",
        _schema(
            {"device": _DEVICE, "interface": {"type": "string", "description": "e.g. eth1"}},
            ("device",),
        ),
        False,
    ),
    ToolSpec(
        "get_routes",
        "Routing table of a device, optionally for one prefix (routers only).",
        _schema(
            {"device": _DEVICE, "prefix": {"type": "string", "description": "e.g. 10.0.0.0/24"}},
            ("device",),
        ),
        False,
    ),
    ToolSpec(
        "get_neighbors",
        "OSPF neighbors or BGP summary of a router.",
        _schema({"device": _DEVICE, "protocol": {"type": "string", "enum": ["ospf", "bgp"]}},
                ("device", "protocol")),
        False,
    ),
    ToolSpec(
        "get_config",
        "Running configuration of a router.",
        _schema({"device": _DEVICE}, ("device",)),
        False,
    ),
    ToolSpec(
        "diff_against_golden",
        "Unified diff between a router's known-good (golden) config and its running config.",
        _schema({"device": _DEVICE}, ("device",)),
        False,
    ),
    ToolSpec(
        "ping",
        "Ping an IPv4 address from a device.",
        _schema({"device": _DEVICE, "target": _TARGET}, ("device", "target")),
        False,
    ),
    ToolSpec(
        "traceroute",
        "Traceroute to an IPv4 address from a device.",
        _schema({"device": _DEVICE, "target": _TARGET}, ("device", "target")),
        False,
    ),
    ToolSpec(
        "propose_change",
        "Record a proposed configuration change for human review. Does not touch the device. "
        "Commands are configuration lines only; entering config mode and committing are "
        "handled by NetMedic. Returns JSON with the change_id.",
        _schema(
            {
                "device": _DEVICE,
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Config-mode commands, e.g. ['interface eth2', 'no shutdown'].",
                },
                "reason": {"type": "string", "description": "Why this change fixes the fault."},
            },
            ("device", "commands", "reason"),
        ),
        False,
    ),
    ToolSpec(
        "apply_change",
        "Apply a proposed change after human approval. Saves the running config for rollback.",
        _schema({"change_id": _CHANGE_ID}, ("change_id",)),
        True,
    ),
    ToolSpec(
        "rollback_change",
        "Restore the config saved before an applied change, after human approval.",
        _schema({"change_id": _CHANGE_ID}, ("change_id",)),
        True,
    ),
)

_SPECS_BY_NAME = {spec.name: spec for spec in SPECS}


def _check_arguments(spec: ToolSpec, arguments: dict) -> None:
    props = spec.parameters["properties"]
    extra = sorted(set(arguments) - set(props))
    if extra:
        raise ToolError(f"unexpected argument(s) for {spec.name}: {', '.join(extra)}")
    missing = [name for name in spec.parameters["required"] if name not in arguments]
    if missing:
        raise ToolError(f"missing argument(s) for {spec.name}: {', '.join(missing)}")
    for name, value in arguments.items():
        schema = props[name]
        if schema["type"] == "string" and not isinstance(value, str):
            raise ToolError(f"argument '{name}' must be a string")
        if schema["type"] == "array" and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            raise ToolError(f"argument '{name}' must be a list of strings")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolError(f"argument '{name}' must be one of {schema['enum']}")


class Toolbox:
    def __init__(
        self,
        inventory: Inventory,
        driver: Driver,
        audit: AuditLog,
        golden_dir: Path,
        changes_dir: Path,
        approver: Approver | None = None,
        allow_changes: bool = True,
    ):
        self.inventory = inventory
        self.driver = driver
        self.audit = audit
        self.golden_dir = Path(golden_dir)
        self.changes_dir = Path(changes_dir)
        self.approver = approver
        self.allow_changes = allow_changes
        self._approved: bool | None = None
        self._audit_device: str | None = None

    def specs(self) -> list[ToolSpec]:
        return [s for s in SPECS if self.allow_changes or not s.is_change]

    def call(self, name: str, arguments: dict) -> str:
        """Run one tool. Never raises: failures come back as 'ERROR: <reason>'."""
        self._approved = None
        args = arguments if isinstance(arguments, dict) else {}
        device = args.get("device")
        self._audit_device = device if isinstance(device, str) else None
        try:
            result = truncate(self._dispatch(name, arguments))
        except NetMedicError as exc:
            result = f"ERROR: {exc}"
        except Exception as exc:  # driver or unexpected failure; never escape to the agent
            result = f"ERROR: {type(exc).__name__}: {exc}"
        try:
            logged_args = arguments if isinstance(arguments, dict) else {"_raw": repr(arguments)}
            self.audit.record(name, logged_args, self._audit_device, len(result), self._approved)
        except Exception as exc:
            result = f"ERROR: audit log write failed: {exc}"
        return result

    # --- dispatch -------------------------------------------------------------

    def _dispatch(self, name: str, arguments: object) -> str:
        spec = _SPECS_BY_NAME.get(name)
        if spec is None:
            raise ToolError(f"unknown tool '{name}'")
        if spec.is_change and not self.allow_changes:
            raise ToolError(f"{name} is disabled (changes are not allowed in this session)")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be a JSON object")
        _check_arguments(spec, arguments)
        return getattr(self, f"_tool_{name}")(**arguments)

    def _run(self, device: Device, command: str) -> str:
        validate_read_command(command, device.platform)
        return self.driver.run(device, command)

    @staticmethod
    def _require_router(device: Device, tool: str) -> PlatformCommands:
        if device.platform not in ROUTER_PLATFORMS:
            raise ToolError(
                f"{tool} is not supported on {device.name} (platform {device.platform})"
            )
        return commands_for(device.platform)

    @staticmethod
    def _running_config_command(device: Device) -> str:
        command = commands_for(device.platform).running_config
        if command is None:
            raise ToolError(f"{device.name} (platform {device.platform}) has no running config")
        return command

    # --- read-only tools ------------------------------------------------------

    def _tool_list_devices(self) -> str:
        devices = [
            {"name": d.name, "platform": d.platform, "role": d.role}
            for d in self.inventory.devices.values()
        ]
        return json.dumps(devices, indent=2)

    def _tool_run_show(self, device: str, command: str) -> str:
        dev = self.inventory.get(device)
        return self._run(dev, command.strip())

    def _tool_get_interfaces(self, device: str, interface: str | None = None) -> str:
        dev = self.inventory.get(device)
        cmds = commands_for(dev.platform)
        if interface is None:
            return self._run(dev, cmds.interfaces)
        if cmds.interface is None:
            raise ToolError(
                f"interface filter is not supported on {dev.platform} hosts; omit it"
            )
        if not INTERFACE_RE.match(interface):
            raise ToolError(f"invalid interface name '{interface}'")
        return self._run(dev, cmds.interface.format(name=interface))

    def _tool_get_routes(self, device: str, prefix: str | None = None) -> str:
        dev = self.inventory.get(device)
        cmds = commands_for(dev.platform)
        if prefix is None:
            return self._run(dev, cmds.routes)
        if cmds.route is None:
            raise ToolError(f"prefix filter is not supported on {dev.platform} hosts; omit it")
        try:
            ipaddress.IPv4Network(prefix, strict=False)
        except ValueError:
            raise ToolError(f"invalid IPv4 prefix '{prefix}'") from None
        return self._run(dev, cmds.route.format(prefix=prefix))

    def _tool_get_neighbors(self, device: str, protocol: str) -> str:
        dev = self.inventory.get(device)
        cmds = self._require_router(dev, "get_neighbors")
        command = cmds.ospf_neighbors if protocol == "ospf" else cmds.bgp_neighbors
        return self._run(dev, command)

    def _tool_get_config(self, device: str) -> str:
        dev = self.inventory.get(device)
        self._require_router(dev, "get_config")
        return self._run(dev, self._running_config_command(dev))

    def _tool_diff_against_golden(self, device: str) -> str:
        dev = self.inventory.get(device)
        self._require_router(dev, "diff_against_golden")
        golden_path = self.golden_dir / f"{dev.name}.cfg"
        if not golden_path.is_file():
            raise ToolError(f"no golden config for {dev.name} ({golden_path.name})")
        golden_text = clean_running_config(golden_path.read_text(encoding="utf-8"))
        running_text = clean_running_config(self._run(dev, self._running_config_command(dev)))
        golden = [line.rstrip() for line in golden_text.splitlines()]
        running = [line.rstrip() for line in running_text.splitlines()]
        diff = list(
            difflib.unified_diff(
                golden, running, f"golden/{dev.name}.cfg", f"{dev.name} running-config",
                lineterm="",
            )
        )
        return "\n".join(diff) if diff else "No differences"

    def _probe(self, tool: str, device: str, target: str) -> str:
        dev = self.inventory.get(device)
        validate_ipv4(target)
        return self.driver.run(dev, probe_command(dev.platform, tool, target))

    def _tool_ping(self, device: str, target: str) -> str:
        return self._probe("ping", device, target)

    def _tool_traceroute(self, device: str, target: str) -> str:
        return self._probe("traceroute", device, target)

    # --- change tools -----------------------------------------------------------

    def _path(self, change_id: str) -> Path:
        if not CHANGE_ID_RE.match(change_id):
            raise ToolError(f"invalid change_id '{change_id}'")
        return self.changes_dir / f"{change_id}.json"

    def _load(self, change_id: str) -> ChangeProposal:
        path = self._path(change_id)
        if not path.is_file():
            raise ToolError(f"unknown change_id '{change_id}'")
        proposal = ChangeProposal(**json.loads(path.read_text(encoding="utf-8")))
        self._audit_device = proposal.device
        return proposal

    def _save(self, proposal: ChangeProposal) -> None:
        self.changes_dir.mkdir(parents=True, exist_ok=True)
        self._path(proposal.id).write_text(json.dumps(asdict(proposal), indent=2), "utf-8")

    def _approve(self, proposal: ChangeProposal) -> None:
        if self.approver is None:
            self._approved = False
            raise ToolError("no approver is configured; changes cannot be applied")
        try:
            self._approved = bool(self.approver(proposal))
        except ApprovalPending:
            self._approved = False  # status is left unchanged so the call can be retried
            raise
        if not self._approved:
            raise ToolError(f"change {proposal.id} was not approved")

    def _tool_propose_change(self, device: str, commands: list[str], reason: str) -> str:
        dev = self.inventory.get(device)
        commands = [c.strip() for c in commands if c.strip()]
        validate_change_commands(commands, dev.platform)
        proposal = ChangeProposal(
            id=uuid.uuid4().hex[:12], device=dev.name, commands=commands, reason=reason,
            status="proposed", rollback_config=None,
        )
        self._save(proposal)
        return json.dumps(
            {"change_id": proposal.id, "device": dev.name, "commands": commands,
             "status": proposal.status}
        )

    def _tool_apply_change(self, change_id: str) -> str:
        proposal = self._load(change_id)
        if proposal.status != "proposed":
            raise ToolError(f"change {change_id} is '{proposal.status}', expected 'proposed'")
        dev = self.inventory.get(proposal.device)
        proposal.rollback_config = clean_running_config(
            self.driver.run(dev, self._running_config_command(dev))
        )
        try:
            self._approve(proposal)
        except ToolError:
            if self._approved is False and self.approver is not None:
                proposal.status = "denied"
                self._save(proposal)
            raise
        output = self.driver.configure(dev, proposal.commands)
        proposal.status = "applied"
        self._save(proposal)
        return f"Change {change_id} applied to {dev.name}.\n{output}"

    def _tool_rollback_change(self, change_id: str) -> str:
        proposal = self._load(change_id)
        if proposal.status != "applied":
            raise ToolError(f"change {change_id} is '{proposal.status}', expected 'applied'")
        if not proposal.rollback_config:
            raise ToolError(f"change {change_id} has no saved rollback config")
        dev = self.inventory.get(proposal.device)
        self._approve(proposal)
        output = self.driver.restore_config(dev, proposal.rollback_config)
        proposal.status = "rolled_back"
        self._save(proposal)
        return f"Change {change_id} rolled back on {dev.name}.\n{output}"
