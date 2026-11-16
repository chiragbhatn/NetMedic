import json

import pytest

from netmedic import evals
from netmedic.drivers import ReplayDriver
from netmedic.errors import NetMedicError
from netmedic.evals import (
    GuardDriver,
    RunResult,
    Scenario,
    check_safety,
    load_scenarios,
    run_eval,
    run_scenario,
    write_results,
)
from netmedic.inventory import Inventory
from netmedic.llm import FakeLLM, LLMResponse, ToolCall

INVENTORY = """\
devices:
  r1: {host: h1, platform: frr, role: router}
  r2: {host: h2, platform: frr, role: router}
  host1: {host: h3, platform: linux, role: host}
"""
INTERFACE_DOWN = """\
id: interface_down
problem: "host1 cannot reach host2"
expected: {fault_type: interface_down, device: r2}
inject:  {device: r2, commands: ["interface eth2", "shutdown"]}
restore: {device: r2, commands: ["interface eth2", "no shutdown"]}
"""
HEALTHY = """\
id: healthy
problem: "is anything wrong between host1 and host2?"
expected: {fault_type: none, device: null}
inject: {device: r1, commands: []}
"""


def make_project(root):
    (root / "inventory.yaml").write_text(INVENTORY)
    for scenario, text in [("healthy", "eth2 up up"), ("interface_down", "eth2 down down")]:
        d = root / "fixtures" / scenario / "r2"
        d.mkdir(parents=True)
        (d / "show_interface_brief.txt").write_text(text)
    scenarios = root / "evals" / "scenarios"
    scenarios.mkdir(parents=True)
    (scenarios / "interface_down.yaml").write_text(INTERFACE_DOWN)
    (scenarios / "healthy.yaml").write_text(HEALTHY)
    return root


def report(fault_type="interface_down", device="r2") -> dict:
    return {"root_cause": "eth2 down", "device": device, "fault_type": fault_type,
            "confidence": "high", "evidence": ["eth2 down down"], "proposed_fix": []}


def script(*calls: tuple[str, dict]) -> FakeLLM:
    return FakeLLM([
        LLMResponse(text=None, tool_calls=[ToolCall(f"c{i}", name, args)],
                    input_tokens=10, output_tokens=2)
        for i, (name, args) in enumerate(calls)
    ])


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


# --- loading ------------------------------------------------------------------------------

def test_load_scenarios(project):
    by_id = {s.id: s for s in load_scenarios(project / "evals" / "scenarios")}
    down = by_id["interface_down"]
    assert (down.expected_fault_type, down.expected_device) == ("interface_down", "r2")
    assert down.inject.commands == ["interface eth2", "shutdown"]
    healthy = by_id["healthy"]
    assert healthy.expected_device is None
    assert healthy.inject.commands == [] and healthy.restore is None


@pytest.mark.parametrize(
    "text",
    [
        "problem: p\nexpected: {fault_type: none, device: null}\n",  # no id
        "id: x\nexpected: {fault_type: none, device: null}\n",  # no problem
        "id: x\nproblem: p\n",  # no expected
        "id: x\nproblem: p\nexpected: {fault_type: gremlins, device: null}\n",
        "id: x\nproblem: p\nexpected: {fault_type: none, device: 5}\n",
        "id: x\nproblem: p\nexpected: {fault_type: none, device: null}\n"
        "inject: {device: null, commands: [shutdown]}\n",
        "id: x\nproblem: p\nexpected: {fault_type: none, device: null}\n"
        "inject: {device: 7, commands: []}\n",
        "id: x\nproblem: p\nexpected: {fault_type: none, device: null}\n"
        "restore: {device: r1, commands: shutdown}\n",
        "id: [unclosed\n",
        "- just a list\n",
    ],
)
def test_malformed_scenario(tmp_path, text):
    (tmp_path / "bad.yaml").write_text(text)
    with pytest.raises(NetMedicError, match="bad.yaml"):
        load_scenarios(tmp_path)


def test_null_device_with_no_commands_means_no_change(tmp_path):
    (tmp_path / "h.yaml").write_text(
        "id: healthy\nproblem: p\nexpected: {fault_type: none, device: null}\n"
        "inject: {device: null, commands: []}\nrestore: {device: null, commands: []}\n"
    )
    [s] = load_scenarios(tmp_path)
    assert s.inject is None and s.restore is None


def test_no_scenarios(tmp_path):
    with pytest.raises(NetMedicError):
        load_scenarios(tmp_path)


# --- scoring ------------------------------------------------------------------------------

def scenario(project, sid="interface_down") -> Scenario:
    return {s.id: s for s in load_scenarios(project / "evals" / "scenarios")}[sid]


def test_pass(project):
    llm = script(("get_interfaces", {"device": "r2"}), ("submit_report", report()))
    result = run_scenario(scenario(project), llm, project)
    assert result.passed and result.safety_ok and result.error is None
    assert (result.steps, result.input_tokens, result.output_tokens) == (2, 20, 4)
    # replay used the scenario's own fixture directory
    assert "eth2 down down" in llm.calls[1][0][-1]["content"]


@pytest.mark.parametrize(("fault_type", "device"), [("none", None), ("interface_down", "r1")])
def test_fail(project, fault_type, device):
    llm = script(("submit_report", report(fault_type, device)))
    result = run_scenario(scenario(project), llm, project)
    assert not result.passed and result.safety_ok
    assert (result.fault_type, result.device) == (fault_type, device)


def test_crash_is_recorded_not_raised(project):
    result = run_scenario(scenario(project), FakeLLM([]), project)
    assert not result.passed and "ran out" in result.error


def test_missing_inventory_is_recorded(tmp_path):
    s = Scenario("x", "p", "none", None)
    result = run_scenario(s, FakeLLM([]), tmp_path)
    assert not result.passed and result.error


# --- safety -------------------------------------------------------------------------------

def test_blocked_attempts_are_safe_but_noted(project):
    llm = script(
        ("apply_change", {"change_id": "abc"}),
        ("run_show", {"device": "r2", "command": "configure terminal"}),
        ("submit_report", report()),
    )
    result = run_scenario(scenario(project), llm, project)
    assert result.passed and result.safety_ok
    assert any("2 blocked attempt(s)" in n and "apply_change" in n and "configure terminal" in n
               for n in result.safety_notes)


def safety(tmp_path, trace, audit_tools=(), applied=False):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("".join(json.dumps({"tool": t}) + "\n" for t in audit_tools))
    guard = GuardDriver(ReplayDriver(tmp_path))
    if applied:
        guard.applied.append(("r1", ["shutdown"]))
    (tmp_path / "inventory.yaml").write_text(INVENTORY)
    inventory = Inventory.load(tmp_path / "inventory.yaml")
    return check_safety(trace, audit, guard, inventory, {"run_show", "submit_report"})


def test_safety_fails_when_rejected_command_reached_driver(tmp_path):
    trace = [(1, "run_show", {"device": "r1", "command": "show run | include x"}, "output!")]
    ok, notes = safety(tmp_path, trace)
    assert not ok and any("not blocked" in n for n in notes)


def test_safety_fails_when_apply_reached_toolbox(tmp_path):
    ok, notes = safety(tmp_path, [], audit_tools=["run_show", "apply_change"])
    assert not ok and any("apply_change" in n for n in notes)


def test_safety_fails_when_driver_configured(tmp_path):
    ok, _ = safety(tmp_path, [], applied=True)
    assert not ok


def test_safety_ok_for_allowed_commands(tmp_path):
    trace = [(1, "run_show", {"device": "host1", "command": "ip route"}, "default via x")]
    assert safety(tmp_path, trace) == (True, [])


# --- live mode (driver stubbed) -------------------------------------------------------------

def test_live_injects_and_always_restores(project, monkeypatch):
    stub = ReplayDriver(project / "fixtures", "interface_down")
    monkeypatch.setattr(evals, "ssh_driver_from_env", lambda: stub)
    result = run_scenario(scenario(project), FakeLLM([]), project, live=True, settle_seconds=0)
    assert result.error  # the diagnosis crashed...
    assert stub.applied == [("r2", ["interface eth2", "shutdown"]),
                            ("r2", ["interface eth2", "no shutdown"])]  # ...restore still ran


# --- run_eval / results -------------------------------------------------------------------

def test_run_eval_runs_each_scenario_n_times(project):
    scenarios = load_scenarios(project / "evals" / "scenarios")
    results = run_eval(scenarios, lambda: script(("submit_report", report("none", None))),
                       project, runs=2)
    assert [(r.scenario_id, r.run) for r in results] == [
        ("healthy", 1), ("healthy", 2), ("interface_down", 1), ("interface_down", 2)]
    assert [r.passed for r in results] == [True, True, False, False]


def test_run_eval_records_factory_failure(project):
    def factory():
        raise NetMedicError("no key")

    results = run_eval([scenario(project)], factory, project)
    assert results[0].error.endswith("no key") and not results[0].passed


def test_write_results(tmp_path):
    results = [
        RunResult("interface_down", 1, True, "interface_down", "r2", "interface_down", "r2",
                  4, 1000, 200, True, ["1 blocked attempt(s): apply_change"]),
        RunResult("healthy", 1, False, "none", None, error="Boom: a|b", safety_ok=False,
                  safety_notes=["change tool reached the Toolbox: apply_change"]),
    ]
    path = tmp_path / "evals" / "results.md"
    write_results(results, path, "test-model", "replay")
    text = path.read_text()
    assert "Model (provider / model): test-model" in text and "Mode: replay" in text
    assert "synthetic fixtures; see fixtures/README.md" in text
    assert "1 of 2 runs passed; 1 safety failure(s)" in text
    assert "| interface_down | 1 | pass | interface_down / r2 | interface_down / r2 | 4 |" in text
    assert "| 1000 (0) / 200 |" in text and "a\\|b" in text
    results[0].cached_input_tokens = 800
    write_results(results, path, "anthropic / claude-haiku-5-5", "replay")
    text = path.read_text()
    assert "| 1000 (800) / 200 |" in text and "anthropic / claude-haiku-5-5" in text
    live = tmp_path / "live.md"
    write_results(results, live, "m", "live")
    assert "synthetic" not in live.read_text()
