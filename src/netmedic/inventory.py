"""Device inventory, from YAML (default) or NetBox."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from netmedic.errors import NetMedicError, UnknownDeviceError

DEFAULT_INVENTORY = "inventory.yaml"
DEFAULT_NETBOX_TAG = "netmedic"


@dataclass(frozen=True)
class Device:
    name: str
    host: str
    platform: str
    role: str
    port: int = 22


class Inventory:
    def __init__(
        self,
        devices: dict[str, Device],
        links: list[tuple[str, str]] | None = None,
        source: str = "",
        warnings: list[str] | None = None,
    ):
        self.devices = devices
        self.links = links or []  # (device, device) pairs, e.g. for drawing the topology
        self.source = source  # where the devices came from, without credentials
        self.warnings = warnings or []  # skipped entries, for the user (stderr)

    @classmethod
    def load(cls, path: Path) -> Inventory:
        """Load `devices: {name: {host, platform, role, port?}}` and optional
        `links: [[a, b], ...]` from a YAML file."""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        devices = {
            name: Device(
                name=name,
                host=str(fields["host"]),
                platform=str(fields["platform"]),
                role=str(fields["role"]),
                port=int(fields.get("port", 22)),
            )
            for name, fields in (data.get("devices") or {}).items()
        }
        links, warnings = [], []
        for pair in data.get("links") or []:
            if (isinstance(pair, list | tuple) and len(pair) == 2
                    and all(isinstance(n, str) and n in devices for n in pair)):
                links.append((pair[0], pair[1]))
            else:
                warnings.append(f"ignored link {pair!r}: needs two known device names")
        return cls(devices, links=links, source=str(path), warnings=warnings)

    def get(self, name: str) -> Device:
        try:
            return self.devices[name]
        except KeyError:
            known = ", ".join(sorted(self.devices))
            raise UnknownDeviceError(f"unknown device '{name}' (known: {known})") from None


def load_inventory(root: Path) -> Inventory:
    """The configured inventory: NETMEDIC_INVENTORY is a YAML path (default
    inventory.yaml, relative to `root`) or the literal `netbox`."""
    choice = (os.environ.get("NETMEDIC_INVENTORY") or DEFAULT_INVENTORY).strip()
    if choice.lower() != "netbox":
        path = Path(choice)
        path = path if path.is_absolute() else Path(root) / path
        if not path.is_file():
            raise NetMedicError(f"inventory file not found: {path}")
        return Inventory.load(path)
    url = os.environ.get("NETBOX_URL", "").strip()
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    for name, value in (("NETBOX_URL", url), ("NETBOX_TOKEN", token)):
        if not value:
            raise NetMedicError(f"NETMEDIC_INVENTORY=netbox needs {name}")
    tag = os.environ.get("NETBOX_TAG")
    tag = DEFAULT_NETBOX_TAG if tag is None else tag.strip()
    from netmedic.netbox import load_from_netbox

    return load_from_netbox(url, token, tag=tag)
