import json
from types import SimpleNamespace

import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage

from netmedic.agent import (
    FAULT_TYPES,
    NUDGE,
    SUBMIT_REPORT_TOOL,
    SYSTEM_PROMPT,
    run_diagnosis,
)
from netmedic.audit import AuditLog
from netmedic.drivers import ReplayDriver, slugify
from netmedic.inventory import Device, Inventory
from netmedic.llm import AnthropicClient, FakeLLM, LLMResponse, ToolCall
from netmedic.tools.toolbox import Toolbox

REPORT = {
    "root_cause": "r2 eth2 is administratively down",
    "device": "r2",
    "fault_type": "interface_down",
    "confidence": "high",
    "evidence": ["eth2  down  down"],
    "proposed_fix": ["interface eth2", "no shutdown"],
}


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "fixtures" / "healthy" / "r2"
    d.mkdir(parents=True)
    (d / f"{slugify('show interface brief')}.txt").write_text("eth2  down  down")
    (d / f"{slugify('show running-config')}.txt").write_text("hostname r2\n")
    inventory = Inventory({
        "r1": Device("r1", "h1", "frr", "router"),
        "r2": Device("r2", "h2", "frr", "router"),
    })
    driver = ReplayDriver(tmp_path / "fixtures")
    toolbox = Toolbox(inventory, driver, AuditLog(tmp_path / "audit.jsonl"),
                      tmp_path / "golden", tmp_path / "changes", approver=lambda p: True)
    return tmp_path, driver, toolbox


def call(cid: str, name: str, **arguments) -> ToolCall:
    return ToolCall(cid, name, arguments)


def respond(*calls: ToolCall, text: str | None = None, tokens=(10, 5)) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls),
                       input_tokens=tokens[0], output_tokens=tokens[1])


def audit_tools(tmp_path) -> list[str]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["tool"] for line in path.read_text().splitlines()]


def test_happy_path_and_accounting(env):
    tmp_path, _, toolbox = env
    llm = FakeLLM([
        respond(call("c1", "get_interfaces", device="r2"), tokens=(100, 20)),
        respond(call("c2", "submit_report", **REPORT), tokens=(150, 40)),
    ])
    steps = []
    report = run_diagnosis("host1 cannot reach host2", toolbox, llm,
                           on_step=lambda *a: steps.append(a))
    assert report.fault_type == "interface_down" and report.device == "r2"
    assert report.proposed_fix == ["interface eth2", "no shutdown"]
    assert (report.steps_taken, report.input_tokens, report.output_tokens) == (2, 250, 60)
    assert steps[0] == (1, "get_interfaces", {"device": "r2"}, "eth2  down  down")
    assert steps[1][:2] == (2, "submit_report")
    assert audit_tools(tmp_path) == ["get_interfaces"]

    messages, tools = llm.calls[1]
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[1] == {"role": "user", "content": "host1 cannot reach host2"}
    assert messages[-1] == {"role": "tool", "tool_call_id": "c1", "content": "eth2  down  down"}
    names = {t["name"] for t in tools}
    assert {"submit_report", "propose_change", "run_show"} <= names
    assert not names & {"apply_change", "rollback_change"}
    submit = next(t for t in tools if t["name"] == "submit_report")
    assert submit["parameters"]["properties"]["fault_type"]["enum"] == list(FAULT_TYPES)


@pytest.mark.parametrize(
    "bad",
    [
        {**REPORT, "fault_type": "cosmic_rays"},
        {**REPORT, "device": "r9"},
        {**REPORT, "confidence": "certain"},
        {k: v for k, v in REPORT.items() if k != "evidence"},
        {**REPORT, "evidence": "eth2 down"},
    ],
)
def test_invalid_report_is_bounced_then_accepted(env, bad):
    _, _, toolbox = env
    llm = FakeLLM([
        respond(call("c1", "submit_report", **bad)),
        respond(call("c2", "submit_report", **{**REPORT, "device": None, "fault_type": "none"})),
    ])
    report = run_diagnosis("p", toolbox, llm)
    assert report.fault_type == "none" and report.device is None
    assert report.steps_taken == 2
    bounced = llm.calls[1][0][-1]
    assert bounced["tool_call_id"] == "c1" and bounced["content"].startswith("ERROR: invalid")


def test_plain_text_gets_nudge(env):
    _, _, toolbox = env
    llm = FakeLLM([
        respond(text="I think it is r2."),
        respond(call("c1", "submit_report", **REPORT)),
    ])
    report = run_diagnosis("p", toolbox, llm)
    assert report.steps_taken == 2
    assert llm.calls[1][0][-1] == {"role": "user", "content": NUDGE}


def test_budget_exhaustion_returns_unknown(env):
    _, _, toolbox = env
    llm = FakeLLM([respond(call(f"c{i}", "list_devices")) for i in range(3)])
    report = run_diagnosis("p", toolbox, llm, max_steps=3)
    assert report.fault_type == "unknown" and report.confidence == "low"
    assert report.device is None and "budget" in report.root_cause
    assert (report.steps_taken, report.input_tokens, report.output_tokens) == (3, 30, 15)


@pytest.mark.parametrize("tool", ["apply_change", "rollback_change", "made_up_tool"])
def test_unoffered_tool_is_refused(env, tool):
    tmp_path, driver, toolbox = env
    llm = FakeLLM([
        respond(call("c1", tool, change_id="abc123")),
        respond(call("c2", "submit_report", **REPORT)),
    ])
    results = []
    run_diagnosis("p", toolbox, llm, on_step=lambda s, n, a, r: results.append(r))
    assert results[0].startswith("ERROR: ") and "not available" in results[0]
    assert audit_tools(tmp_path) == []
    assert driver.applied == [] and driver.restored == []


def test_propose_then_apply_attempt_never_applies(env):
    tmp_path, driver, toolbox = env
    llm = FakeLLM([
        respond(call("c1", "propose_change", device="r2",
                     commands=["interface eth2", "no shutdown"], reason="down")),
        respond(call("c2", "apply_change", change_id="whatever")),
        respond(call("c3", "submit_report", **REPORT)),
    ])
    run_diagnosis("p", toolbox, llm)
    assert audit_tools(tmp_path) == ["propose_change"]
    assert driver.applied == []


def test_multiple_tool_calls_in_one_step(env):
    tmp_path, _, toolbox = env
    llm = FakeLLM([
        respond(call("c1", "list_devices"), call("c2", "get_interfaces", device="r2")),
        respond(call("c3", "submit_report", **REPORT)),
    ])
    steps = []
    report = run_diagnosis("p", toolbox, llm, on_step=lambda *a: steps.append(a[:2]))
    assert steps == [(1, "list_devices"), (1, "get_interfaces"), (2, "submit_report")]
    assert report.steps_taken == 2
    assert audit_tools(tmp_path) == ["list_devices", "get_interfaces"]


UNSUPPORTED_IN_STRICT = {"minimum", "maximum", "multipleOf", "minLength", "maxLength",
                         "minItems", "maxItems", "uniqueItems", "pattern"}


def _check_strict_schema(schema: dict, path: str = "") -> None:
    assert not isinstance(schema.get("type"), list), f"{path}: list-valued type"
    assert not UNSUPPORTED_IN_STRICT & set(schema), f"{path}: unsupported keyword"
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False, path
        assert sorted(schema["required"]) == sorted(schema["properties"]), path
        for name, sub in schema["properties"].items():
            _check_strict_schema(sub, f"{path}.{name}")
    if schema.get("type") == "array":
        _check_strict_schema(schema["items"], f"{path}[]")
    for i, variant in enumerate(schema.get("anyOf", [])):
        _check_strict_schema(variant, f"{path}.anyOf[{i}]")


def test_submit_report_schema_is_strict_compatible():
    assert SUBMIT_REPORT_TOOL["strict"] is True
    _check_strict_schema(SUBMIT_REPORT_TOOL["parameters"])
    device = SUBMIT_REPORT_TOOL["parameters"]["properties"]["device"]
    assert device["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_only_submit_report_is_strict(env):
    _, _, toolbox = env
    llm = FakeLLM([respond(call("c1", "submit_report", **REPORT))])
    run_diagnosis("p", toolbox, llm)
    tools = llm.calls[0][1]
    assert [t["name"] for t in tools if t.get("strict")] == ["submit_report"]


MERGED_ROOT_CAUSE = ("Interface eth2 on r2 is admin down.</root_cause>\n"
                     '<parameter name="device">r2')


def test_merged_parameters_get_an_actionable_bounce(env):
    _, _, toolbox = env
    merged = {k: v for k, v in REPORT.items() if k != "device"}
    merged["root_cause"] = MERGED_ROOT_CAUSE
    results = []
    llm = FakeLLM([
        respond(call("c1", "submit_report", **merged)),
        respond(call("c2", "submit_report", **REPORT)),
    ])
    report = run_diagnosis("p", toolbox, llm, on_step=lambda s, n, a, r: results.append(r))
    assert results[0] == (
        "ERROR: invalid report: field 'device' is missing and 'root_cause' contains merged "
        "parameter markup; send every field as its own JSON argument, with no XML tags "
        "inside values"
    )
    assert report.root_cause == REPORT["root_cause"]  # accepted only the clean retry


@pytest.mark.parametrize(
    "polluted",
    [
        {"root_cause": 'eth2 down</parameter>\n<parameter name="fault_type">interface_down'},
        {"evidence": ["eth2 down down", "x</evidence>"]},
        {"proposed_fix": ["interface eth2", '<parameter name="x">no shutdown']},
    ],
)
def test_markup_in_values_is_rejected_even_with_all_fields(env, polluted):
    _, _, toolbox = env
    results = []
    llm = FakeLLM([
        respond(call("c1", "submit_report", **{**REPORT, **polluted})),
        respond(call("c2", "submit_report", **REPORT)),
    ])
    report = run_diagnosis("p", toolbox, llm, on_step=lambda s, n, a, r: results.append(r))
    assert results[0].startswith("ERROR: invalid report: ")
    assert "merged parameter markup" in results[0] and "is missing" not in results[0]
    assert report.steps_taken == 2  # never repaired: only the clean report is accepted


def test_two_missing_fields_with_markup(env):
    _, _, toolbox = env
    merged = {k: v for k, v in REPORT.items() if k not in ("device", "fault_type")}
    merged["root_cause"] = MERGED_ROOT_CAUSE
    results = []
    llm = FakeLLM([respond(call("c1", "submit_report", **merged)),
                   respond(call("c2", "submit_report", **REPORT))])
    run_diagnosis("p", toolbox, llm, on_step=lambda s, n, a, r: results.append(r))
    assert "fields 'device', 'fault_type' are missing and 'root_cause' contains" in results[0]


def test_assistant_turn_keeps_provider_raw(env):
    _, _, toolbox = env
    raw = [{"type": "thinking", "thinking": "", "signature": "s"}]
    llm = FakeLLM([
        LLMResponse(None, [call("c1", "list_devices")], 1, 1, raw=raw),
        respond(call("c2", "submit_report", **REPORT)),
    ])
    run_diagnosis("p", toolbox, llm)
    assistant = llm.calls[1][0][2]
    assert assistant["role"] == "assistant" and assistant["provider_raw"] is raw
    assert assistant["tool_calls"][0].name == "list_devices"


def test_cached_tokens_are_summed(env):
    _, _, toolbox = env
    llm = FakeLLM([
        LLMResponse(None, [call("c1", "list_devices")], 100, 5, cached_input_tokens=0),
        LLMResponse(None, [call("c2", "submit_report", **REPORT)], 200, 5,
                    cached_input_tokens=90),
    ])
    report = run_diagnosis("p", toolbox, llm)
    assert (report.input_tokens, report.cached_input_tokens) == (300, 90)


# --- end to end with the real AnthropicClient on claude-haiku-5-5 (stubbed SDK client) ------

class RecordingMessages:
    """Stands in for `client.messages`: replays scripted Message objects, records requests."""

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses[len(self.requests) - 1]


def haiku_message(content, stop_reason="tool_use", cache_read=0) -> Message:
    return Message.model_construct(
        id="msg", type="message", role="assistant", model="claude-haiku-5-5", content=content,
        stop_reason=stop_reason, stop_details=None,
        usage=Usage(input_tokens=40, output_tokens=10, cache_creation_input_tokens=0,
                    cache_read_input_tokens=cache_read),
    )


def test_haiku_end_to_end_echoes_turns_verbatim(env, monkeypatch):
    for var in ("NETMEDIC_EFFORT", "NETMEDIC_ANTHROPIC_FALLBACKS"):
        monkeypatch.delenv(var, raising=False)
    tmp_path, _, toolbox = env
    thinking1 = ThinkingBlock(type="thinking", thinking="", signature="sig-1")
    turn1 = [thinking1,
             ToolUseBlock(type="tool_use", id="toolu_a", name="get_interfaces",
                          input={"device": "r2"}),
             ToolUseBlock(type="tool_use", id="toolu_b", name="run_show",
                          input={"device": "r2", "command": "configure terminal"})]
    turn2 = [TextBlock(type="text", text="I have enough evidence.")]
    turn3 = [ThinkingBlock(type="thinking", thinking="", signature="sig-3"),
             ToolUseBlock(type="tool_use", id="toolu_c", name="submit_report", input=REPORT)]
    messages_api = RecordingMessages([
        haiku_message(turn1),
        haiku_message(turn2, "end_turn", cache_read=900),
        haiku_message(turn3, cache_read=1000),
    ])
    beta = SimpleNamespace(messages=SimpleNamespace(create=None))  # must never be used
    client = SimpleNamespace(messages=messages_api, beta=beta)
    llm = AnthropicClient(model="claude-haiku-5-5", client=client)

    report = run_diagnosis("host1 cannot reach host2", toolbox, llm)

    assert (report.fault_type, report.device, report.steps_taken) == ("interface_down", "r2", 3)
    assert (report.input_tokens, report.cached_input_tokens) == (3 * 40 + 1900, 1900)
    assert audit_tools(tmp_path) == ["get_interfaces", "run_show"]
    first, second, third = messages_api.requests
    for request in (first, second, third):
        assert request["model"] == "claude-haiku-5-5" and "betas" not in request
        assert request["system"] == SYSTEM_PROMPT  # byte-stable across steps
        assert request["tools"] == first["tools"]
        assert request["cache_control"] == {"type": "ephemeral"}
    assert first["messages"] == [{"role": "user", "content": "host1 cannot reach host2"}]
    # step 2: turn 1 echoed as the same objects (thinking included), both results in one message
    assert second["messages"][1] == {"role": "assistant", "content": turn1}
    assert second["messages"][1]["content"][0] is thinking1
    results = second["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_a", "toolu_b"]
    assert "is_error" not in results[0] and results[1]["is_error"] is True
    # step 3: the earlier prefix is unchanged (append-only), plus turn 2 and the nudge
    assert third["messages"][:3] == second["messages"]
    assert third["messages"][3] == {"role": "assistant", "content": turn2}
    assert third["messages"][4] == {"role": "user", "content": NUDGE}
