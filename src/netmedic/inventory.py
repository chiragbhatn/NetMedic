"""Device inventory loaded from YAML."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from netmedic.errors import UnknownDeviceError


@dataclass(frozen=True)
class Device:
    name: str
    host: str
    platform: str
    role: str
    port: int = 22


class Inventory:
    def __init__(self, devices: dict[str, Device]):
        self.devices = devices

    @classmethod
    def load(cls, path: Path) -> Inventory:
        """Load `devices: {name: {host, platform, role, port?}}` from a YAML file."""
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
        return cls(devices)

    def get(self, name: str) -> Device:
        try:
            return self.devices[name]
        except KeyError:
            known = ", ".join(sorted(self.devices))
            raise UnknownDeviceError(f"unknown device '{name}' (known: {known})") from None
