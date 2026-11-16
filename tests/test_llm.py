from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage
from anthropic.types.beta.beta_fallback_block import BetaFallbackBlock
from anthropic.types.refusal_stop_details import RefusalStopDetails

from netmedic.errors import NetMedicError
from netmedic.llm import (
    ANTHROPIC_FALLBACK_MODELS,
    AnthropicClient,
    FakeLLM,
    LLMResponse,
    ToolCall,
    create_llm,
    describe_llm,
    provider_error_message,
)

TOOLS = [{"name": "ping", "description": "Ping.", "parameters": {
    "type": "object", "properties": {"target": {"type": "string"}},
    "required": ["target"], "additionalProperties": False}}]


def test_fake_llm_records_and_runs_out():
    first = LLMResponse(text="a", tool_calls=[], input_tokens=1, output_tokens=2)
    fake = FakeLLM([first])
    assert fake.complete([{"role": "user", "content": "x"}], TOOLS) is first
    assert fake.calls == [([{"role": "user", "content": "x"}], TOOLS)]
    with pytest.raises(NetMedicError, match="ran out"):
        fake.complete([], [])


# --- Anthropic ----------------------------------------------------------------------------

THINKING = ThinkingBlock(type="thinking", thinking="", signature="sig-abc")
CALL = ToolUseBlock(type="tool_use", id="toolu_1", name="ping",
                    input={"device": "r1", "target": "10.0.0.1"})


def anthropic_message(content, stop_reason="tool_use", usage=None, stop_details=None) -> Message:
    usage = usage or Usage(input_tokens=50, output_tokens=20, cache_creation_input_tokens=300,
                           cache_read_input_tokens=1200)
    return Message.model_construct(
        id="msg_1", type="message", role="assistant", model="claude-haiku-5-5",
        content=content, stop_reason=stop_reason, stop_details=stop_details, usage=usage,
    )


class StubCreate:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.kwargs = response, exc, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


def anthropic_stub(response=None, exc=None) -> SimpleNamespace:
    return SimpleNamespace(messages=StubCreate(response, exc),
                           beta=SimpleNamespace(messages=StubCreate(response, exc)))


@pytest.fixture
def no_anthropic_env(monkeypatch):
    for var in ("NETMEDIC_EFFORT", "NETMEDIC_ANTHROPIC_FALLBACKS", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def claude(model="claude-haiku-5-5", response=None, exc=None, **kwargs) -> AnthropicClient:
    return AnthropicClient(model=model, client=anthropic_stub(response, exc), **kwargs)


def test_anthropic_requires_key(no_anthropic_env):
    with pytest.raises(NetMedicError, match="ANTHROPIC_API_KEY"):
        AnthropicClient(model="claude-haiku-5-5")


def test_anthropic_rejects_bad_effort(no_anthropic_env, monkeypatch):
    monkeypatch.setenv("NETMEDIC_EFFORT", "extreme")
    with pytest.raises(NetMedicError, match="NETMEDIC_EFFORT"):
        claude()


def test_anthropic_request_shape(no_anthropic_env):
    llm = claude(response=anthropic_message([TextBlock(type="text", text="ok")], "end_turn"))
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "problem"},
        {"role": "assistant", "content": None, "tool_calls": [ToolCall("toolu_1", "ping", {})],
         "provider_raw": [THINKING, CALL]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "pong"},
        {"role": "assistant", "content": "two calls",
         "tool_calls": [ToolCall("t2", "get_config", {"device": "r1"}),
                        ToolCall("t3", "run_show", {"device": "r1", "command": "reload"})]},
        {"role": "tool", "tool_call_id": "t2", "content": "hostname r1"},
        {"role": "tool", "tool_call_id": "t3", "content": "ERROR: command rejected"},
        {"role": "user", "content": "keep going"},
    ]
    llm.complete(messages, TOOLS)
    kwargs = llm.client.messages.kwargs
    assert llm.client.beta.messages.kwargs is None  # haiku: no fallbacks, plain endpoint
    assert set(kwargs) == {"model", "max_tokens", "system", "messages", "tools", "cache_control"}
    assert kwargs["model"] == "claude-haiku-5-5" and kwargs["max_tokens"] == 16000
    assert kwargs["system"] == "sys"
    assert kwargs["cache_control"] == {"type": "ephemeral"}
    assert kwargs["tools"] == [{"name": "ping", "description": "Ping.",
                                "input_schema": TOOLS[0]["parameters"]}]
    sent = kwargs["messages"]
    assert sent[0] == {"role": "user", "content": "problem"}
    # provider_raw echoed verbatim: the very same block objects, thinking included
    assert sent[1]["role"] == "assistant"
    assert sent[1]["content"][0] is THINKING and sent[1]["content"][1] is CALL
    assert sent[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "pong"}]}
    # no provider_raw: rebuilt from text + tool calls
    assert sent[3] == {"role": "assistant", "content": [
        {"type": "text", "text": "two calls"},
        {"type": "tool_use", "id": "t2", "name": "get_config", "input": {"device": "r1"}},
        {"type": "tool_use", "id": "t3", "name": "run_show",
         "input": {"device": "r1", "command": "reload"}},
    ]}
    # both results of one turn in ONE user message, errors flagged
    assert sent[4] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": "hostname r1"},
        {"type": "tool_result", "tool_use_id": "t3", "content": "ERROR: command rejected",
         "is_error": True},
    ]}
    assert sent[5] == {"role": "user", "content": "keep going"}
    assert len(sent) == 6
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert forbidden not in kwargs


def test_anthropic_conversion_does_not_edit_history(no_anthropic_env):
    messages = [{"role": "user", "content": "p"},
                {"role": "assistant", "content": None, "tool_calls": [], "provider_raw": [CALL]},
                {"role": "tool", "tool_call_id": "toolu_1", "content": "a"}]
    before = repr(messages)
    first = AnthropicClient.to_messages(messages)
    messages.append({"role": "assistant", "content": "x", "tool_calls": []})
    second = AnthropicClient.to_messages(messages)
    assert repr(messages[:3]) == before
    assert second[1][:3] == first[1]  # earlier turns convert identically: prefix is stable


STRICT_TOOL = {"name": "submit_report", "description": "Report.", "strict": True,
               "parameters": {"type": "object", "properties": {"a": {"type": "string"}},
                              "required": ["a"], "additionalProperties": False}}


def test_strict_flag_carried_to_tool_definition(no_anthropic_env):
    anthropic_tools = AnthropicClient.to_tools([TOOLS[0], STRICT_TOOL])
    assert "strict" not in anthropic_tools[0]
    assert anthropic_tools[1] == {"name": "submit_report", "description": "Report.",
                                  "input_schema": STRICT_TOOL["parameters"], "strict": True}


def test_anthropic_effort(no_anthropic_env, monkeypatch):
    monkeypatch.setenv("NETMEDIC_EFFORT", "low")
    llm = claude(response=anthropic_message([], "end_turn"))
    llm.complete([{"role": "user", "content": "p"}], [])
    assert llm.client.messages.kwargs["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("model", ANTHROPIC_FALLBACK_MODELS)
def test_anthropic_fallbacks_on_for_listed_models(no_anthropic_env, model):
    llm = claude(model, response=anthropic_message([], "end_turn"))
    llm.complete([{"role": "user", "content": "p"}], [])
    kwargs = llm.client.beta.messages.kwargs
    assert llm.client.messages.kwargs is None
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"


@pytest.mark.parametrize(("model", "env"), [("claude-haiku-5-5", None),
                                             ("claude-opus-5-5", "off"),
                                             ("claude-opus-5-5", "OFF")])
def test_anthropic_fallbacks_off(no_anthropic_env, monkeypatch, model, env):
    if env:
        monkeypatch.setenv("NETMEDIC_ANTHROPIC_FALLBACKS", env)
    llm = claude(model, response=anthropic_message([], "end_turn"))
    llm.complete([{"role": "user", "content": "p"}], [])
    assert llm.client.beta.messages.kwargs is None
    assert "fallbacks" not in llm.client.messages.kwargs
    assert "betas" not in llm.client.messages.kwargs


def test_anthropic_response_parsing(no_anthropic_env):
    fallback = BetaFallbackBlock.model_construct(type="fallback")
    content = [fallback, THINKING, TextBlock(type="text", text="Checking r1."), CALL,
               SimpleNamespace(type="some_future_block")]
    result = claude(response=anthropic_message(content)).complete([], [])
    assert result.text == "Checking r1."
    assert result.tool_calls == [
        ToolCall("toolu_1", "ping", {"device": "r1", "target": "10.0.0.1"})
    ]
    assert result.raw == content  # every block kept for the echo, unknown ones included
    assert result.input_tokens == 50 + 300 + 1200
    assert result.cached_input_tokens == 1200
    assert result.output_tokens == 20


def test_anthropic_usage_without_cache_fields(no_anthropic_env):
    usage = Usage(input_tokens=10, output_tokens=3)
    result = claude(response=anthropic_message([], "end_turn", usage)).complete([], [])
    assert (result.input_tokens, result.cached_input_tokens, result.text) == (10, 0, None)


def test_anthropic_refusal(no_anthropic_env):
    details = RefusalStopDetails(type="refusal", category="cyber")
    response = anthropic_message([CALL], "refusal", stop_details=details)
    with pytest.raises(NetMedicError, match="refused.*cyber"):
        claude(response=response).complete([], [])
    response = anthropic_message([], "refusal")
    with pytest.raises(NetMedicError, match="refused.*unspecified"):
        claude(response=response).complete([], [])


@pytest.mark.parametrize("stop", ["max_tokens", "model_context_window_exceeded"])
def test_anthropic_truncated_response_is_not_executed(no_anthropic_env, stop):
    with pytest.raises(NetMedicError, match="cut off"):
        claude(response=anthropic_message([CALL], stop)).complete([], [])


ANTHROPIC_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def anthropic_status(cls, status, message):
    return cls(f"Error code: {status}", response=httpx2.Response(status, request=ANTHROPIC_REQUEST),
               body={"type": "error", "error": {"type": "x", "message": message}})


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (anthropic_status(anthropic.BadRequestError, 400, "messages: roles must alternate"),
         "Anthropic API error (HTTP 400): messages: roles must alternate"),
        (anthropic_status(anthropic.AuthenticationError, 401, "invalid x-api-key"),
         "Anthropic API error (HTTP 401): invalid x-api-key"),
        (anthropic_status(anthropic.PermissionDeniedError, 403, "not allowed"),
         "Anthropic API error (HTTP 403): not allowed"),
        (anthropic_status(anthropic.NotFoundError, 404, "model: claude-nope"),
         "Anthropic API error (HTTP 404): model: claude-nope"),
        (anthropic_status(anthropic.RateLimitError, 429, "Your credit balance is too low."),
         "Anthropic API error (HTTP 429): Your credit balance is too low."),
        (anthropic_status(anthropic.InternalServerError, 500, "Internal server error"),
         "Anthropic API error (HTTP 500): Internal server error"),
        (anthropic_status(anthropic.OverloadedError, 529, "Overloaded"),
         "Anthropic API error (HTTP 529): Overloaded"),
        (anthropic.APIConnectionError(request=ANTHROPIC_REQUEST),
         "Anthropic API error: Connection error."),
    ],
)
def test_anthropic_errors_become_netmedic_errors(no_anthropic_env, exc, expected):
    with pytest.raises(NetMedicError) as info:
        claude(exc=exc).complete([], [])
    assert str(info.value) == expected
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_anthropic_timeout_and_key_masking(no_anthropic_env):
    with pytest.raises(NetMedicError, match="^Anthropic API error: Request timed out"):
        claude(exc=anthropic.APITimeoutError(request=ANTHROPIC_REQUEST)).complete([], [])
    leaked = anthropic_status(anthropic.AuthenticationError, 401,
                              "invalid key sk-ant-api03-SECRETSECRET-abc")
    with pytest.raises(NetMedicError) as info:
        claude(exc=leaked).complete([], [])
    assert "SECRET" not in str(info.value) and "sk-***" in str(info.value)


# --- construction ---------------------------------------------------------------------------

def test_create_llm_requires_model(no_anthropic_env, monkeypatch):
    monkeypatch.delenv("NETMEDIC_MODEL", raising=False)
    with pytest.raises(NetMedicError, match="NETMEDIC_MODEL"):
        create_llm()


def test_create_llm_is_anthropic(no_anthropic_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("NETMEDIC_MODEL", "claude-haiku-5-5")
    llm = create_llm()
    assert isinstance(llm, AnthropicClient)
    assert describe_llm(llm) == "anthropic / claude-haiku-5-5"
    # a non-Claude id is passed through as-is; the API decides whether it exists
    assert create_llm("some-other-model").model == "some-other-model"
    assert describe_llm(FakeLLM([])) == "FakeLLM"


def test_provider_error_message_defaults_to_anthropic():
    assert provider_error_message(RuntimeError("boom")) == "Anthropic API error: boom"
