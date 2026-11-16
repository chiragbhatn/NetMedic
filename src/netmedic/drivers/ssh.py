"""Live driver over SSH using Netmiko. Not covered by tests (needs the lab)."""

from __future__ import annotations

import base64
import shlex
from typing import Any

from netmedic.errors import NetMedicError
from netmedic.inventory import Device

DEVICE_TYPES = {"frr": "linux", "arista_eos": "arista_eos", "linux": "linux"}
SHELL_COMMANDS = ("ping ", "traceroute ")
FRR_ROLLBACK_FILE = "/tmp/netmedic-rollback.conf"


def _vtysh(*commands: str) -> str:
    return "vtysh " + " ".join(f"-c {shlex.quote(c)}" for c in commands)


class SSHDriver:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    def _connect(self, device: Device) -> Any:
        from netmiko import ConnectHandler  # lazy: tests never need a network

        return ConnectHandler(
            device_type=DEVICE_TYPES[device.platform],
            host=device.host,
            port=device.port,
            username=self.username,
            password=self.password,
        )

    def run(self, device: Device, command: str) -> str:
        if device.platform == "frr" and not command.startswith(SHELL_COMMANDS):
            command = _vtysh(command)
        elif device.platform in ("frr", "linux") and command.startswith("ping "):
            # Linux ping runs forever without a count.
            command = "ping -c 3 -W 1 " + command.removeprefix("ping ")
        with self._connect(device) as conn:
            return conn.send_command(command, read_timeout=60)

    def configure(self, device: Device, commands: list[str]) -> str:
        with self._connect(device) as conn:
            if device.platform == "frr":
                return conn.send_command(_vtysh("configure terminal", *commands))
            return conn.send_config_set(commands)

    def restore_config(self, device: Device, config: str) -> str:
        """Replace the running config with `config`.

        Unverified until the live lab exists. EOS uses a config session with
        `rollback clean-config`; FRR copies the file to the device and runs frr-reload.py.
        """
        if device.platform == "linux":
            raise NetMedicError(f"restore_config is not supported on {device.name} (linux)")
        with self._connect(device) as conn:
            if device.platform == "arista_eos":
                session = ["configure session netmedic-rollback", "rollback clean-config"]
                return conn.send_config_set(
                    [*session, *config.splitlines(), "commit"],
                    enter_config_mode=False, exit_config_mode=False, cmd_verify=False,
                )
            encoded = base64.b64encode(config.encode()).decode()
            conn.send_command(f"echo {encoded} | base64 -d > {FRR_ROLLBACK_FILE}")
            return conn.send_command(
                f"/usr/lib/frr/frr-reload.py --reload {FRR_ROLLBACK_FILE}", read_timeout=120
            )
