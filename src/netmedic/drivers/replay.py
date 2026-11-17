"""Replay driver: answers commands from recorded output in fixtures/ (SPEC section 7)."""

from __future__ import annotations

import re
from pathlib import Path

from netmedic.errors import FixtureNotFoundError
from netmedic.inventory import Device

FALLBACK_SCENARIO = "healthy"


def slugify(command: str) -> str:
    """`show ip route 192.168.3.0/24` -> `show_ip_route_192_168_3_0_24`."""
    return re.sub(r"[^a-z0-9]+", "_", command.lower()).strip("_")


class ReplayDriver:
    def __init__(self, fixtures_dir: Path, scenario: str = FALLBACK_SCENARIO):
        self.fixtures_dir = Path(fixtures_dir)
        self.scenario = scenario
        self.applied: list[tuple[str, list[str]]] = []
        self.restored: list[tuple[str, str]] = []

    def run(self, device: Device, command: str) -> str:
        filename = slugify(command) + ".txt"
        for scenario in dict.fromkeys([self.scenario, FALLBACK_SCENARIO]):
            path = self.fixtures_dir / scenario / device.name / filename
            try:
                found = path.is_file()
            except OSError:  # e.g. file name too long on Linux: there is no such fixture
                found = False
            if found:
                return path.read_text(encoding="utf-8")
        raise FixtureNotFoundError(
            f"no fixture for '{command}' on {device.name} "
            f"(scenario '{self.scenario}', looked for {filename})"
        )

    def configure(self, device: Device, commands: list[str]) -> str:
        self.applied.append((device.name, list(commands)))
        return f"replay: recorded {len(commands)} config command(s) for {device.name}"

    def restore_config(self, device: Device, config: str) -> str:
        self.restored.append((device.name, config))
        return f"replay: recorded config restore for {device.name}"
