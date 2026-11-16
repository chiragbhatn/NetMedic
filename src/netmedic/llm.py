"""LLM client (Anthropic Claude) behind a small provider-neutral interface.

Neutral message format used by the agent loop:
  {"role": "system" | "user", "content": str}
  {"role": "assistant", "content": str | None, "tool_calls": [ToolCall, ...],
   "provider_raw": Any}      # provider's own form of the turn, echoed back verbatim
  {"role": "tool", "tool_call_id": str, "content": str}
Tools are {"name", "description", "parameters", "strict"?} dicts (parameters = JSON
Schema; "strict": True asks the provider to guarantee arguments match the schema).
History is append-only: clients convert it, never edit it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from netmedic.errors import NetMedicError

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0  # all input tokens, cached or not
    output_tokens: int = 0
    raw: Any = None  # provider's own representation of the assistant turn
    cached_input_tokens: int = 0  # part of input_tokens served from the prompt cache


class LLMClient(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


def _require_model(model: str | None) -> str:
    model = model or os.environ.get("NETMEDIC_MODEL")
    if not model:
        raise NetMedicError("no model configured: set NETMEDIC_MODEL or pass model=")
    return model


_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-*]{4,}")


def provider_error_message(exc: Exception, provider: str = "Anthropic") -> str:
    """Short, key-free message: provider, HTTP status if any, and the provider's own text."""
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "api_message", None)  # error.message from the body
    body = getattr(exc, "body", None)
    if not message and isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        message = inner.get("message")
    if not isinstance(message, str) or not message:
        message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    message = _SECRET_RE.sub("sk-***", " ".join(message.split()))
    where = f"{provider} API error (HTTP {status})" if status else f"{provider} API error"
    return f"{where}: {message}"


# --- Anthropic ----------------------------------------------------------------------------

ANTHROPIC_MAX_TOKENS = 16000
# Models with server-side refusal fallbacks (`fallbacks="default"`); others (e.g. Haiku 5.5)
# have none, and sending the parameter would at best do nothing.
ANTHROPIC_FALLBACK_MODELS = (
    "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
)
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    """Anthropic Messages API client.

    Assistant turns are sent back exactly as received (`provider_raw`), thinking blocks
    included, because the API rejects edited history. Never sends sampling parameters,
    `thinking`, a forced `tool_choice` or an assistant prefill.
    """

    provider = "anthropic"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        client: Any = None,
        effort: str | None = None,
        fallbacks: bool | None = None,
    ):
        self.model = _require_model(model)
        self.effort = effort or os.environ.get("NETMEDIC_EFFORT") or None
        if self.effort and self.effort not in EFFORT_LEVELS:
            raise NetMedicError(f"NETMEDIC_EFFORT must be one of {', '.join(EFFORT_LEVELS)}")
        if fallbacks is None:
            fallbacks = os.environ.get("NETMEDIC_ANTHROPIC_FALLBACKS", "").lower() != "off"
        self.fallbacks = fallbacks and self.model in ANTHROPIC_FALLBACK_MODELS
        if client is None:
            if not (api_key or os.environ.get("ANTHROPIC_API_KEY")):
                raise NetMedicError("no API key configured: set ANTHROPIC_API_KEY or pass api_key=")
            import anthropic  # lazy: tests and replay mode never need it

            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.client = client

    @staticmethod
    def to_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        """Neutral messages -> (system prompt, Messages API `messages`)."""
        system: list[str] = []
        out: list[dict] = []
        for m in messages:
            role = m["role"]
            if role == "system":
                system.append(m["content"])
            elif role == "user":
                out.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                content = m.get("provider_raw")
                if content is None:  # e.g. a turn scripted by FakeLLM: rebuild it
                    content = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                    content += [
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                        for tc in m.get("tool_calls") or []
                    ]
                out.append({"role": "assistant", "content": content})
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"],
                         "content": m["content"]}
                if m["content"].startswith("ERROR: "):
                    block["is_error"] = True
                previous = out[-1]["content"] if out and out[-1]["role"] == "user" else None
                if isinstance(previous, list) and previous[-1].get("type") == "tool_result":
                    previous.append(block)  # all results of one turn go in one user message
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                raise NetMedicError(f"unknown message role '{role}'")
        return "\n\n".join(system), out

    @staticmethod
    def to_tools(tools: list[dict]) -> list[dict]:
        converted = []
        for t in tools:
            tool = {"name": t["name"], "description": t["description"],
                    "input_schema": t["parameters"]}
            if t.get("strict"):
                tool["strict"] = True  # strict tool use: input guaranteed to match the schema
            converted.append(tool)
        return converted

    def request(self, messages: list[dict], tools: list[dict]) -> dict:
        """The keyword arguments for one create() call (no credentials in here)."""
        system, converted = self.to_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": system,
            "messages": converted,
            "tools": self.to_tools(tools),
            "cache_control": {"type": "ephemeral"},  # automatic caching of the growing prefix
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            kwargs["betas"] = [ANTHROPIC_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    @staticmethod
    def parse(response: Any) -> LLMResponse:
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise NetMedicError(f"Anthropic model refused the request (category: {category})")
        if stop in ("max_tokens", "model_context_window_exceeded"):
            raise NetMedicError(
                f"Anthropic response was cut off (stop_reason: {stop}); "
                "its tool calls were not executed"
            )
        texts, calls = [], []
        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                texts.append(block.text)
            elif kind == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(arguments)))
            # thinking, redacted_thinking, fallback and unknown blocks: kept only in `raw`
        usage = response.usage
        uncached = usage.input_tokens or 0
        written = getattr(usage, "cache_creation_input_tokens", None) or 0
        read = getattr(usage, "cache_read_input_tokens", None) or 0
        return LLMResponse(
            text="".join(texts) or None,
            tool_calls=calls,
            input_tokens=uncached + written + read,
            output_tokens=usage.output_tokens or 0,
            raw=list(response.content),
            cached_input_tokens=read,
        )

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        import anthropic

        kwargs = self.request(messages, tools)
        api = self.client.beta.messages if self.fallbacks else self.client.messages
        try:
            response = api.create(**kwargs)
        except anthropic.APIStatusError as exc:  # 4xx/5xx after the SDK's own retries
            raise NetMedicError(provider_error_message(exc, "Anthropic")) from None
        except anthropic.APIConnectionError as exc:  # includes APITimeoutError
            raise NetMedicError(provider_error_message(exc, "Anthropic")) from None
        except anthropic.AnthropicError as exc:  # anything else the SDK raises
            raise NetMedicError(provider_error_message(exc, "Anthropic")) from None
        return self.parse(response)


# --- construction -------------------------------------------------------------------------

def create_llm(model: str | None = None) -> LLMClient:
    """Build the Anthropic client from the environment (NETMEDIC_MODEL required)."""
    return AnthropicClient(_require_model(model))


def describe_llm(llm: object) -> str:
    """e.g. 'anthropic / claude-haiku-5-5'; falls back to the class name."""
    provider = getattr(llm, "provider", None)
    model = getattr(llm, "model", None)
    if provider and model:
        return f"{provider} / {model}"
    return str(model or type(llm).__name__)


# --- tests ---------------------------------------------------------------------------------

class FakeLLM:
    """Scripted LLM for tests: returns `responses` in order and records each call."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.calls.append((list(messages), list(tools)))
        if len(self.calls) > len(self.responses):
            raise NetMedicError(
                f"FakeLLM ran out of scripted responses (call {len(self.calls)}, "
                f"{len(self.responses)} scripted)"
            )
        return self.responses[len(self.calls) - 1]
