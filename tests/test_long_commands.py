"""Over-long commands: rejected by the guardrail, and never leak a filesystem path.

Found by CI: on Linux a 400-character command made the fixture lookup raise
"File name too long" with the full temp path in the tool result.
"""

from pathlib import Path

import pytest

from netmedic.drivers import ReplayDriver
from netmedic.errors import CommandRejected, FixtureNotFoundError
from netmedic.guardrails import MAX_COMMAND_CHARS, validate_read_command
from netmedic.inventory import Device

R2 = Device(name="r2", host="r2", platform="frr", role="router")


def test_command_at_the_limit_is_accepted():
    command = "show " + "x" * (MAX_COMMAND_CHARS - len("show "))
    assert len(command) == MAX_COMMAND_CHARS
    validate_read_command(command, "frr")


def test_command_over_the_limit_is_rejected():
    command = "show " + "interface " * 40
    with pytest.raises(CommandRejected, match="too long"):
        validate_read_command(command, "frr")


def test_replay_lookup_error_is_a_missing_fixture_without_the_path(tmp_path, monkeypatch):
    def too_long(self):
        raise OSError(36, "File name too long", str(self))

    monkeypatch.setattr(Path, "is_file", too_long)
    driver = ReplayDriver(tmp_path, "healthy")
    with pytest.raises(FixtureNotFoundError) as info:
        driver.run(R2, "show ip route")
    assert str(tmp_path) not in str(info.value)
