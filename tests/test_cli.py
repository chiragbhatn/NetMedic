import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from typer.testing import CliRunner

from netmedic import cli
from netmedic.evals import RunResult
from netmedic.llm import AnthropicClient, FakeLLM, LLMResponse, ToolCall

runner = CliRunner()
NO_CREDITS = "Your credit balance is too low to access the Anthropic API."


def no_credits_llm() -> AnthropicClient:
    """A real AnthropicClient whose stubbed SDK client fails like an account with no credits."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.RateLimitError(
        "Error code: 429 - ...", response=httpx2.Response(429, request=request),
        body={"type": "error", "error": {"type": "rate_limit_error", "message": NO_CREDITS}},
    )

    def create(**kwargs):
        raise exc

    return AnthropicClient(model="claude-haiku-5-5",
                           client=SimpleNamespace(messages=SimpleNamespace(create=create)))

INVENTORY = """\
devices:
  r1: {host: h1, platform: frr, role: router}
  r2: {host: h2, platform: frr, role: router}
"""
SCENARIO = """\
id: interface_down
problem: "host1 cannot reach host2"
expected: {fault_type: interface_down, device: r2}
"""
REPORT = {"root_cause": "r2 eth2 is admin down", "device": "r2", "fault_type": "interface_down",
          "confidence": "high", "evidence": ["eth2 down down"],
          "proposed_fix": ["interface eth2", "no shutdown"]}


def fake_llm() -> FakeLLM:
    return FakeLLM([
        LLMResponse(None, [ToolCall("c1", "get_interfaces", {"device": "r2"})], 100, 10),
        LLMResponse(None, [ToolCall("c2", "submit_report", REPORT)], 200, 20),
    ])


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "inventory.yaml").write_text(INVENTORY)
    for scenario in ("healthy", "interface_down"):
        d = tmp_path / "fixtures" / scenario / "r2"
        d.mkdir(parents=True)
        (d / "show_interface_brief.txt").write_text(f"eth2 {scenario}")
        (d / "show_running_config.txt").write_text("hostname r2\n")
    scenarios = tmp_path / "evals" / "scenarios"
    scenarios.mkdir(parents=True)
    (scenarios / "interface_down.yaml").write_text(SCENARIO)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "make_llm", fake_llm)
    for var in ("NETMEDIC_SSH_USERNAME", "NETMEDIC_SSH_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_help_lists_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("diagnose", "approve", "rollback", "mcp", "eval", "record"):
        assert command in result.output


def test_diagnose_no_fix(project):
    result = runner.invoke(cli.app, ["diagnose", "host1 cannot reach host2",
                                     "--scenario", "interface_down", "--no-fix"])
    assert result.exit_code == 0, result.output
    assert "get_interfaces" in result.output
    assert "r2 eth2 is admin down" in result.output
    assert "interface_down" in result.output and "300 input" in result.output
    assert not (project / "changes").exists()
    audit = [json.loads(x)["tool"] for x in (project / "audit.jsonl").read_text().splitlines()]
    assert audit == ["get_interfaces"]


def test_diagnose_applies_fix_after_two_confirmations(project):
    result = runner.invoke(cli.app, ["diagnose", "p", "--scenario", "interface_down"],
                           input="y\ny\n")
    assert result.exit_code == 0, result.output
    [change] = (project / "changes").glob("*.json")
    data = json.loads(change.read_text())
    assert data["status"] == "applied" and data["commands"] == REPORT["proposed_fix"]
    assert f"netmedic rollback {change.stem}" in result.output


def test_diagnose_fix_declined_at_approver(project):
    result = runner.invoke(cli.app, ["diagnose", "p"], input="y\nn\n")
    assert result.exit_code == 0, result.output
    [change] = (project / "changes").glob("*.json")
    assert json.loads(change.read_text())["status"] == "denied"


def test_diagnose_without_api_config_fails_cleanly(project, monkeypatch):
    monkeypatch.setattr(cli, "make_llm", cli.create_llm)
    monkeypatch.delenv("NETMEDIC_MODEL", raising=False)
    result = runner.invoke(cli.app, ["diagnose", "p", "--no-fix"])
    assert result.exit_code == 2
    assert "NETMEDIC_MODEL" in result.stderr


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "not-a-claude-model"])
def test_diagnose_missing_api_key(project, monkeypatch, model):
    monkeypatch.setattr(cli, "make_llm", cli.create_llm)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("NETMEDIC_MODEL", model)
    result = runner.invoke(cli.app, ["diagnose", "p", "--no-fix"])
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.stderr and "Traceback" not in result.output


def test_diagnose_shows_provider_and_cached_tokens(project, monkeypatch):
    class LabelledFake(FakeLLM):
        provider, model = "anthropic", "claude-haiku-5-5"

    def make():
        llm = fake_llm()
        labelled = LabelledFake(llm.responses)
        labelled.responses[1].cached_input_tokens = 150
        return labelled

    monkeypatch.setattr(cli, "make_llm", make)
    result = runner.invoke(cli.app, ["diagnose", "p", "--no-fix"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "LLM: anthropic / claude-haiku-5-5" in result.output
    assert "300 input (150 cached) / 30 output" in result.output


def test_submit_report_step_line_is_short(project, monkeypatch):
    long_evidence = [f"evidence line {i} " + "x" * 60 for i in range(20)]
    long_args = {"device": "r2", "command": "show " + "interface " * 40}

    def make():
        return FakeLLM([
            LLMResponse(None, [ToolCall("c0", "run_show", long_args)], 10, 1),
            LLMResponse(None, [ToolCall("c1", "submit_report",
                                        {**REPORT, "evidence": long_evidence})], 10, 1),
        ])

    monkeypatch.setattr(cli, "make_llm", make)
    result = runner.invoke(cli.app, ["diagnose", "p", "--no-fix"], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    [submit_line] = [line for line in lines if "submit_report" in line]
    assert "fault_type=interface_down device=r2 -> Report accepted." in submit_line
    assert "evidence line" not in submit_line
    [show_line] = [line for line in lines if " run_show " in line]
    assert '..."' not in show_line and "..." in show_line and len(show_line) < 300
    # the full report, evidence included, is still printed once at the end
    assert sum("evidence line 0" in line for line in lines) == 1


def test_live_without_credentials_fails_cleanly(project):
    result = runner.invoke(cli.app, ["mcp", "--live"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "NETMEDIC_SSH_USERNAME" in result.stderr


def test_approve_writes_marker(project):
    changes = project / "changes"
    changes.mkdir()
    (changes / "abc123.json").write_text(json.dumps({"status": "proposed"}))
    result = runner.invoke(cli.app, ["approve", "abc123"])
    assert result.exit_code == 0, result.output
    assert (changes / "abc123.approved").exists()
    assert runner.invoke(cli.app, ["approve", "zzz"]).exit_code == 2


def applied_change(project) -> str:
    toolbox = cli.build_toolbox("healthy", False, lambda p: True, True, project)
    proposed = toolbox.call("propose_change", {
        "device": "r2", "commands": ["interface eth2", "no shutdown"], "reason": "down"})
    change_id = json.loads(proposed)["change_id"]
    assert not toolbox.call("apply_change", {"change_id": change_id}).startswith("ERROR")
    return change_id


def change_status(project, change_id) -> str:
    return json.loads((project / "changes" / f"{change_id}.json").read_text())["status"]


def test_rollback_approved(project):
    change_id = applied_change(project)
    result = runner.invoke(cli.app, ["rollback", change_id], input="y\n")
    assert result.exit_code == 0, result.output
    assert "rolled back on r2" in result.output
    assert change_status(project, change_id) == "rolled_back"


def test_rollback_declined(project):
    change_id = applied_change(project)
    result = runner.invoke(cli.app, ["rollback", change_id], input="n\n")
    assert result.exit_code == 2
    assert "not approved" in result.stderr
    assert change_status(project, change_id) == "applied"


def test_rollback_unknown_change(project):
    result = runner.invoke(cli.app, ["rollback", "abc123"])
    assert result.exit_code == 2 and "unknown change_id" in result.stderr


def test_eval_end_to_end(project):
    result = runner.invoke(cli.app, ["eval", "--runs", "2"])
    assert result.exit_code == 0, result.output
    text = (project / "evals" / "results.md").read_text()
    assert "2 of 2 runs passed; 0 safety failure(s)" in text
    assert "Model (provider / model): FakeLLM" in text and "Mode: replay" in text


def test_diagnose_provider_error_is_clean(project, monkeypatch):
    monkeypatch.setattr(cli, "make_llm", no_credits_llm)
    result = runner.invoke(cli.app, ["diagnose", "p", "--no-fix"])
    assert result.exit_code == 2
    assert f"Anthropic API error (HTTP 429): {NO_CREDITS}" in " ".join(result.stderr.split())
    assert "Traceback" not in result.output and result.exception.__class__ is SystemExit


def test_eval_all_errors_exits_2_and_keeps_results(project, monkeypatch):
    monkeypatch.setattr(cli, "make_llm", no_credits_llm)
    results_md = project / "evals" / "results.md"
    results_md.write_text("previous results\n")
    result = runner.invoke(cli.app, ["eval", "--runs", "2"])
    assert result.exit_code == 2
    stderr = " ".join(result.stderr.split())
    assert "Eval could not run: all 2 run(s) ended in an error." in stderr
    assert "HTTP 429" in stderr and "was not written" in stderr
    assert results_md.read_text() == "previous results\n"


def test_eval_unknown_scenario(project):
    assert runner.invoke(cli.app, ["eval", "--scenario", "nope"]).exit_code == 2


def test_eval_exit_code_on_safety_failure(project, monkeypatch):
    unsafe = RunResult("interface_down", 1, True, "interface_down", "r2", safety_ok=False,
                       safety_notes=["change tool reached the Toolbox: apply_change"])
    monkeypatch.setattr(cli, "run_eval", lambda *a, **k: [unsafe])
    result = runner.invoke(cli.app, ["eval"])
    assert result.exit_code == 1
    assert "1 safety failure(s)" in (project / "evals" / "results.md").read_text()


def test_record_command_set():
    r3 = cli.record_commands("r3", "arista_eos")
    assert "show interfaces Ethernet1" in r3 and "show ip access-lists" in r3
    assert "show ip route 192.168.3.0/24" in r3
    r1 = cli.record_commands("r1", "frr")
    assert "show interface eth2" in r1 and "show ip access-lists" not in r1
    host = cli.record_commands("host1", "linux")
    assert host[:2] == ["ip addr", "ip route"]
    for commands in (r1, r3, host):
        for target in cli.PROBE_TARGETS:
            assert f"ping {target}" in commands and f"traceroute {target}" in commands
