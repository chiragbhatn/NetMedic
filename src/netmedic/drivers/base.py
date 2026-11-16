"""The driver protocol: how the Toolbox talks to devices."""

from __future__ import annotations

from typing import Protocol

from netmedic.inventory import Device


class Driver(Protocol):
    def run(self, device: Device, command: str) -> str:
        """Run one read-only logical command and return its raw output."""
        ...

    def configure(self, device: Device, commands: list[str]) -> str:
        """Apply configuration commands and return the device's response."""
        ...

    def restore_config(self, device: Device, config: str) -> str:
        """Replace the device's running config with `config` (used by rollback)."""
        ...
