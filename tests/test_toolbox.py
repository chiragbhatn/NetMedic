import json

import pytest

from netmedic.approval import approve_change, file_approver
from netmedic.audit import AuditLog
from netmedic.drivers import ReplayDriver, slugify
from netmedic.guardrails import MAX_OUTPUT_CHARS
from netmedic.inventory import Device, Inventory
from netmedic.tools.toolbox import ChangeProposal, Toolbox, clean_running_config

RUNNING = "hostname r1\ninterface eth2\n shutdown\n"
GOLDEN = "hostname r1\ninterface eth2\n no shutdown\n"

FIXTURES = {
    "r1": {
        "show ip route": "r1 routes",
        "show ip route 192.168.3.0/24": "r1 route to host2 lan",
        "show interface brief": "r1 brief",
        "show interface eth1": "r1 eth1 detail",
        "show ip ospf neighbor": "r1 ospf",
        "show ip bgp summary": "r1 bgp",
        "show running-config": RUNNING,
        "ping 192.168.3.10": "r1 ping ok",
        "traceroute 192.168.3.10": "r1 trace",
    },
    "r3": {
        "show ip interface brief": "r3 brief",
        "show interfaces Ethernet1": "r3 Ethernet1 detail",
        "show ip access-lists": "r3 acls",
        "show running-config": "hostname r3\n",
    },
    "host1": {"ip addr": "host1 addrs", "ip route": "host1 routes", "ping 192.168.3.10": "ok"},
}


@pytest.fixture
def env(tmp_path):
    fixtures = tmp_path / "fixtures"
    for device, outputs in FIXTURES.items():
        d = fixtures / "healthy" / device
        d.mkdir(parents=True)
        for command, text in outputs.items():
            (d / f"{slugify(command)}.txt").write_text(text)
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "r1.cfg").write_text(GOLDEN)
    (golden / "r3.cfg").write_text("hostname r3\n")
    inventory = Inventory({
        "r1": Device("r1", "clab-netmedic-r1", "frr", "router"),
        "r3": Device("r3", "clab-netmedic-r3", "arista_eos", "router"),
        "host1": Device("host1", "clab-netmedic-host1", "linux", "host"),
    })
    return tmp_path, inventory, ReplayDriver(fixtures)


def make(env, **kwargs) -> Toolbox:
    tmp_path, inventory, driver = env
    return Toolbox(
        inventory, driver, AuditLog(tmp_path / "audit.jsonl"),
        golden_dir=tmp_path / "golden", changes_dir=tmp_path / "changes", **kwargs,
    )


def audit_lines(env) -> list[dict]:
    path = env[0] / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# --- specs ------------------------------------------------------------------------

def test_specs_are_valid_json_schema_objects(env):
    specs = make(env).specs()
    assert len(specs) == 12
    for spec in specs:
        p = spec.parameters
        assert p["type"] == "object"
        assert p["additionalProperties"] is False
        assert set(p["required"]) <= set(p["properties"])
    assert {s.name for s in specs if s.is_change} == {"apply_change", "rollback_change"}


# --- read-only tools ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("run_show", {"device": "r1", "command": "show ip route"}, "r1 routes"),
        ("run_show", {"device": "host1", "command": "ip addr"}, "host1 addrs"),
        ("get_interfaces", {"device": "r1"}, "r1 brief"),
        ("get_interfaces", {"device": "r1", "interface": "eth1"}, "r1 eth1 detail"),
        ("get_interfaces", {"device": "r3"}, "r3 brief"),
        ("get_interfaces", {"device": "r3", "interface": "Ethernet1"}, "r3 Ethernet1 detail"),
        ("get_interfaces", {"device": "host1"}, "host1 addrs"),
        ("get_routes", {"device": "r1"}, "r1 routes"),
        ("get_routes", {"device": "r1", "prefix": "192.168.3.0/24"}, "r1 route to host2 lan"),
        ("get_routes", {"device": "host1"}, "host1 routes"),
        ("get_neighbors", {"device": "r1", "protocol": "ospf"}, "r1 ospf"),
        ("get_neighbors", {"device": "r1", "protocol": "bgp"}, "r1 bgp"),
        ("get_config", {"device": "r1"}, RUNNING),
        ("ping", {"device": "r1", "target": "192.168.3.10"}, "r1 ping ok"),
        ("traceroute", {"device": "r1", "target": "192.168.3.10"}, "r1 trace"),
    ],
)
def test_read_tools(env, name, arguments, expected):
    assert make(env).call(name, arguments) == expected


def test_list_devices(env):
    devices = json.loads(make(env).call("list_devices", {}))
    assert {d["name"] for d in devices} == {"r1", "r3", "host1"}


def test_diff_against_golden(env):
    tb = make(env)
    diff = tb.call("diff_against_golden", {"device": "r1"})
    assert "- no shutdown" in diff and "+ shutdown" in diff
    assert tb.call("diff_against_golden", {"device": "r3"}) == "No differences"


def test_diff_ignores_frr_preamble(env):
    (env[0] / "fixtures/healthy/r1/show_running_config.txt").write_text(
        "Building configuration...\n\nCurrent configuration:\n" + GOLDEN
    )
    assert make(env).call("diff_against_golden", {"device": "r1"}) == "No differences"


def test_truncates_long_output(env):
    tmp_path, _, _ = env
    (tmp_path / "fixtures/healthy/r1/show_ip_route.txt").write_text("z" * (MAX_OUTPUT_CHARS * 2))
    result = make(env).call("run_show", {"device": "r1", "command": "show ip route"})
    assert "[truncated" in result
    assert len(result) < MAX_OUTPUT_CHARS + 100


# --- failures never raise and are audited ------------------------------------------

class BoomDriver(ReplayDriver):
    def run(self, device, command):
        raise TimeoutError("ssh timed out")


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("no_such_tool", {}),
        ("run_show", {"device": "r1"}),  # missing argument
        ("run_show", {"device": "r1", "command": "show ip route", "x": 1}),  # extra argument
        ("run_show", {"device": "r1", "command": 5}),  # wrong type
        ("run_show", "not a dict"),
        ("get_neighbors", {"device": "r1", "protocol": "isis"}),
        ("run_show", {"device": "r9", "command": "show ip route"}),  # unknown device
        ("run_show", {"device": "r1", "command": "configure terminal"}),  # rejected
        ("run_show", {"device": "r1", "command": "show ip route | include x"}),
        ("run_show", {"device": "r1", "command": "show ip ospf interface"}),  # no fixture
        ("get_neighbors", {"device": "host1", "protocol": "ospf"}),  # unsupported platform
        ("get_config", {"device": "host1"}),
        ("get_routes", {"device": "r1", "prefix": "not-a-prefix"}),
        ("get_interfaces", {"device": "r1", "interface": "eth1; reload"}),
        ("ping", {"device": "r1", "target": "host2"}),
        ("traceroute", {"device": "r1", "target": "10.0.0.300"}),
        ("diff_against_golden", {"device": "r9"}),
        ("apply_change", {"change_id": "../../etc"}),
        ("apply_change", {"change_id": "deadbeef"}),
        ("rollback_change", {"change_id": "deadbeef"}),
        ("propose_change", {"device": "r1", "commands": ["reload"], "reason": "x"}),
        ("propose_change", {"device": "r1", "commands": "no shutdown", "reason": "x"}),
    ],
)
def test_failures_return_error_and_audit_once(env, name, arguments):
    result = make(env).call(name, arguments)
    assert result.startswith("ERROR: ")
    assert len(audit_lines(env)) == 1


def test_missing_golden_is_error(env):
    (env[0] / "golden" / "r1.cfg").unlink()
    assert make(env).call("diff_against_golden", {"device": "r1"}).startswith("ERROR: ")


def test_driver_exception_becomes_error(env):
    tmp_path, inventory, _ = env
    tb = Toolbox(inventory, BoomDriver(tmp_path), AuditLog(tmp_path / "audit.jsonl"),
                 tmp_path / "golden", tmp_path / "changes")
    result = tb.call("run_show", {"device": "r1", "command": "show ip route"})
    assert result.startswith("ERROR: ") and "timed out" in result


def test_every_call_writes_one_audit_line(env):
    tb = make(env)
    tb.call("list_devices", {})
    tb.call("run_show", {"device": "r1", "command": "show ip route"})
    tb.call("run_show", {"device": "r1", "command": "reload"})
    lines = audit_lines(env)
    assert [entry["tool"] for entry in lines] == ["list_devices", "run_show", "run_show"]
    assert lines[1]["device"] == "r1"
    assert lines[1]["result_chars"] == len("r1 routes")


# --- change flow ------------------------------------------------------------------

def propose(tb: Toolbox) -> str:
    result = tb.call("propose_change", {
        "device": "r1", "commands": ["interface eth2", "no shutdown"], "reason": "eth2 is down",
    })
    data = json.loads(result)
    assert data["commands"] == ["interface eth2", "no shutdown"]
    return data["change_id"]


def load(env, change_id) -> dict:
    return json.loads((env[0] / "changes" / f"{change_id}.json").read_text())


def test_propose_apply_rollback(env):
    seen: list[ChangeProposal] = []
    tb = make(env, approver=lambda p: seen.append(p) or True)
    driver = env[2]
    change_id = propose(tb)
    assert load(env, change_id)["status"] == "proposed"
    assert driver.applied == []

    result = tb.call("apply_change", {"change_id": change_id})
    assert not result.startswith("ERROR"), result
    saved = load(env, change_id)
    assert saved["status"] == "applied"
    assert saved["rollback_config"] == RUNNING
    assert driver.applied == [("r1", ["interface eth2", "no shutdown"])]
    assert seen[0].commands == ["interface eth2", "no shutdown"]

    # cannot apply twice
    assert tb.call("apply_change", {"change_id": change_id}).startswith("ERROR: ")

    result = tb.call("rollback_change", {"change_id": change_id})
    assert not result.startswith("ERROR"), result
    assert load(env, change_id)["status"] == "rolled_back"
    assert driver.restored == [("r1", RUNNING)]
    assert len(driver.applied) == 1  # rollback replaces the config, it does not re-apply lines
    assert len(seen) == 2

    lines = audit_lines(env)
    assert [entry["approved"] for entry in lines] == [None, True, None, True]
    assert lines[1]["device"] == "r1"


def test_apply_denied(env):
    tb = make(env, approver=lambda p: False)
    change_id = propose(tb)
    assert tb.call("apply_change", {"change_id": change_id}).startswith("ERROR: ")
    assert load(env, change_id)["status"] == "denied"
    assert env[2].applied == []
    assert audit_lines(env)[-1]["approved"] is False


def test_apply_without_approver(env):
    tb = make(env)
    change_id = propose(tb)
    assert tb.call("apply_change", {"change_id": change_id}).startswith("ERROR: ")
    assert load(env, change_id)["status"] == "proposed"
    assert env[2].applied == []
    assert audit_lines(env)[-1]["approved"] is False


def test_rollback_denied_keeps_applied(env):
    answers = iter([True, False])
    tb = make(env, approver=lambda p: next(answers))
    change_id = propose(tb)
    tb.call("apply_change", {"change_id": change_id})
    assert tb.call("rollback_change", {"change_id": change_id}).startswith("ERROR: ")
    assert load(env, change_id)["status"] == "applied"
    assert len(env[2].applied) == 1
    assert env[2].restored == []


def test_apply_saves_cleaned_running_config(env):
    (env[0] / "fixtures/healthy/r1/show_running_config.txt").write_text(
        "Building configuration...\n\nCurrent configuration:\n!\nfrr version 9.1\nhostname r1\n"
    )
    tb = make(env, approver=lambda p: True)
    change_id = propose(tb)
    tb.call("apply_change", {"change_id": change_id})
    assert load(env, change_id)["rollback_config"] == "!\nfrr version 9.1\nhostname r1\n"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Building configuration...\n\nCurrent configuration:\n!\nhostname r1\n",
         "!\nhostname r1\n"),
        ("\n\nhostname r1", "hostname r1\n"),
        ("! Command: show running-config\n! device: r3\nhostname r3\n",
         "! Command: show running-config\n! device: r3\nhostname r3\n"),
        ("Building configuration...\n\n", ""),
    ],
)
def test_clean_running_config(raw, clean):
    assert clean_running_config(raw) == clean


def test_pending_approval_keeps_status_and_retries_same_id(env):
    changes = env[0] / "changes"
    tb = make(env, approver=file_approver(changes))
    driver = env[2]
    change_id = propose(tb)

    result = tb.call("apply_change", {"change_id": change_id})
    assert result.startswith("ERROR: ") and f"netmedic approve {change_id}" in result
    assert load(env, change_id)["status"] == "proposed"
    assert audit_lines(env)[-1]["approved"] is False
    approve_change(changes, change_id)
    assert not tb.call("apply_change", {"change_id": change_id}).startswith("ERROR")
    assert load(env, change_id)["status"] == "applied"

    result = tb.call("rollback_change", {"change_id": change_id})
    assert result.startswith("ERROR: ") and "awaiting human approval" in result
    assert load(env, change_id)["status"] == "applied"
    assert audit_lines(env)[-1]["approved"] is False
    approve_change(changes, change_id)
    assert not tb.call("rollback_change", {"change_id": change_id}).startswith("ERROR")
    assert load(env, change_id)["status"] == "rolled_back"
    assert len(driver.applied) == 1 and len(driver.restored) == 1


def test_allow_changes_false_hides_and_blocks(env):
    tb = make(env, approver=lambda p: True, allow_changes=False)
    names = {s.name for s in tb.specs()}
    assert "apply_change" not in names and "rollback_change" not in names
    assert "propose_change" in names
    change_id = propose(tb)
    assert tb.call("apply_change", {"change_id": change_id}).startswith("ERROR: ")
    assert tb.call("rollback_change", {"change_id": change_id}).startswith("ERROR: ")
    assert env[2].applied == []
