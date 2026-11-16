"""Live driver over SSH using Netmiko. Not covered by tests (needs the lab)."""

from __future__ import annotations

import base64
import shlex
from typing import Any

from netmedic.errors import NetMedicError
from netmedic.inventory import Device

DEVICE_TYPES = {
    "frr": "linux", "arista_eos": "arista_eos", "nokia_srlinux": "nokia_srl", "linux": "linux",
}
SHELL_COMMANDS = ("ping ", "traceroute ")
FRR_ROLLBACK_FILE = "/tmp/netmedic-rollback.conf"
# Top-level SR Linux trees replaced wholesale on rollback (system is re-set, not deleted,
# so management access survives a partial rollback config).
SRL_REPLACED_TREES = ("interface", "network-instance", "routing-policy")


def _vtysh(*commands: str) -> str:
    return "vtysh " + " ".join(f"-c {shlex.quote(c)}" for c in commands)


def srl_command(command: str) -> str:
    """SR Linux `ping` runs until interrupted: give it a count, like the other platforms."""
    if command.startswith("ping "):
        return command + " -c 3"
    return command


def srl_restore_commands(config: str) -> list[str]:
    """Candidate-mode commands that replace r4's config with an `info flat` dump.

    UNVERIFIED on a live SR Linux: delete the routing trees, then re-apply every
    `set / ...` line from the saved config; the caller commits once, atomically.
    """
    lines = [line.strip() for line in config.splitlines()]
    sets = [line for line in lines if line.startswith("set / ")]
    if not sets:
        raise NetMedicError("SR Linux rollback config has no 'set / ...' lines")
    return [f"delete / {tree}" for tree in SRL_REPLACED_TREES] + sets


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
        elif device.platform == "nokia_srlinux":
            command = srl_command(command)
        with self._connect(device) as conn:
            return conn.send_command(command, read_timeout=60)

    def configure(self, device: Device, commands: list[str]) -> str:
        with self._connect(device) as conn:
            if device.platform == "frr":
                return conn.send_command(_vtysh("configure terminal", *commands))
            if device.platform == "nokia_srlinux":
                return self._srl_commit(conn, commands)
            return conn.send_config_set(commands)

    @staticmethod
    def _srl_commit(conn: Any, commands: list[str]) -> str:
        """UNVERIFIED: Netmiko's nokia_srl enters `enter candidate private`; `commit now`
        applies the candidate and leaves candidate mode."""
        output = conn.send_config_set(commands, exit_config_mode=False, cmd_verify=False)
        output += conn.send_command("commit now", expect_string=r"[>#]")
        return output

    def restore_config(self, device: Device, config: str) -> str:
        """Replace the running config with `config`.

        Unverified until the live lab exists. EOS uses a config session with
        `rollback clean-config`; FRR copies the file to the device and runs frr-reload.py;
        SR Linux deletes and re-sets its routing trees in one candidate commit.
        """
        if device.platform == "linux":
            raise NetMedicError(f"restore_config is not supported on {device.name} (linux)")
        if device.platform == "nokia_srlinux":
            commands = srl_restore_commands(config)
            with self._connect(device) as conn:
                return self._srl_commit(conn, commands)
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
