"""In-process tests of the MCP server via the SDK's `Client(server)` transport."""

import json

import anyio
import pytest
from mcp import Client

from netmedic.approval import approve_change, file_approver
from netmedic.audit import AuditLog
from netmedic.drivers import ReplayDriver
from netmedic.inventory import Device, Inventory
from netmedic.mcp_server import build_server
from netmedic.tools.toolbox import Toolbox


@pytest.fixture
def setup(tmp_path):
    d = tmp_path / "fixtures" / "healthy" / "r1"
    d.mkdir(parents=True)
    (d / "show_ip_route.txt").write_text("r1 routes")
    (d / "show_running_config.txt").write_text("hostname r1\n")
    inventory = Inventory({"r1": Device("r1", "clab-netmedic-r1", "frr", "router")})
    driver = ReplayDriver(tmp_path / "fixtures")

    def make(**kwargs) -> Toolbox:
        return Toolbox(inventory, driver, AuditLog(tmp_path / "audit.jsonl"),
                       tmp_path / "golden", tmp_path / "changes", **kwargs)

    return tmp_path, driver, make


def run(toolbox: Toolbox, script):
    async def main():
        async with Client(build_server(toolbox)) as client:
            return await script(client)

    return anyio.run(main)


def test_tool_list_matches_specs(setup):
    _, _, make = setup
    for allow in (True, False):
        toolbox = make(allow_changes=allow)
        tools = run(toolbox, lambda c: c.list_tools()).tools
        expected = {s.name: s for s in toolbox.specs()}
        assert [t.name for t in tools] == list(expected)
        for tool in tools:
            assert tool.description == expected[tool.name].description
            assert tool.input_schema == expected[tool.name].parameters
        names = {t.name for t in tools}
        assert ("apply_change" in names) is allow
        assert ("rollback_change" in names) is allow


def test_successful_call(setup):
    _, _, make = setup
    result = run(make(), lambda c: c.call_tool(
        "run_show", {"device": "r1", "command": "show ip route"}))
    assert result.is_error is False
    assert result.content[0].text == "r1 routes"


def test_error_call(setup):
    _, _, make = setup
    result = run(make(), lambda c: c.call_tool(
        "run_show", {"device": "r1", "command": "configure terminal"}))
    assert result.is_error is True
    assert result.content[0].text.startswith("ERROR: ")


def test_apply_with_file_approver(setup):
    tmp_path, driver, make = setup
    changes = tmp_path / "changes"
    toolbox = make(allow_changes=True, approver=file_approver(changes))

    async def script(client):
        proposed = await client.call_tool("propose_change", {
            "device": "r1", "commands": ["interface eth2", "no shutdown"], "reason": "down",
        })
        change_id = json.loads(proposed.content[0].text)["change_id"]
        first = await client.call_tool("apply_change", {"change_id": change_id})
        assert first.is_error is True
        assert f"netmedic approve {change_id}" in first.content[0].text
        # pending, not denied: the same id works once the human approves
        approve_change(changes, change_id)
        second = await client.call_tool("apply_change", {"change_id": change_id})
        assert second.is_error is False, second.content[0].text
        rollback = await client.call_tool("rollback_change", {"change_id": change_id})
        assert rollback.is_error is True  # needs its own marker
        assert "awaiting human approval" in rollback.content[0].text
        approve_change(changes, change_id)
        rollback = await client.call_tool("rollback_change", {"change_id": change_id})
        assert rollback.is_error is False, rollback.content[0].text

    run(toolbox, script)
    assert driver.applied == [("r1", ["interface eth2", "no shutdown"])]
    assert driver.restored == [("r1", "hostname r1\n")]
