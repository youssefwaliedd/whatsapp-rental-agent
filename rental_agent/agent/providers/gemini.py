"""Google Gemini adapter.

The agent loop talks to a small duck-typed client surface — `messages.create`,
`messages.parse`, and blocks with `.type` / `.text` / `.name` / `.input` / `.id`.
This module presents that surface on top of `google-genai`, so the loop, the
tool layer, the engine and every test stay unchanged.

Everything below is translation. The interesting differences from the surface
the loop expects:

* **No mid-conversation system role.** Gemini has one system instruction. The
  per-turn state snapshot is folded in as a clearly marked user part instead.
* **No tool-call ids.** Gemini matches a function response to its call by name,
  so ids are synthesised and the name is encoded into them for the round trip.
* **No server-side refusal fallbacks.** `supports_fallbacks` is False, so the
  agent never attempts the beta path.

The conversion functions are pure and separately tested — no API key required to
verify the translation is correct.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import types

#: Free-tier default. Override with GEMINI_MODEL — run `/models` in the console
#: to see what your key can actually reach.
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

#: Marker for content that is operator instruction rather than customer speech.
STATE_MARKER = "[SYSTEM NOTE — not from the customer, do not quote it back]"

_FINISH_REASONS = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "BLOCKLIST": "refusal",
    "SPII": "refusal",
    "RECITATION": "refusal",
    "IMAGE_SAFETY": "refusal",
    "MALFORMED_FUNCTION_CALL": "end_turn",
}


# --------------------------------------------------------------------------
# Block types the agent loop understands
# --------------------------------------------------------------------------


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str
    type: str = "tool_use"


@dataclass
class Response:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"


@dataclass
class ParsedResponse:
    parsed_output: Any
    stop_reason: str = "end_turn"


# --------------------------------------------------------------------------
# Pure conversions
# --------------------------------------------------------------------------


def encode_call_id(name: str, index: int) -> str:
    """Gemini gives no call ids, but a function response is matched by name.
    Encoding the name into the synthetic id lets the round trip recover it."""
    return f"fc:{index}:{name}"


def decode_call_id(call_id: str) -> str:
    parts = call_id.split(":", 2)
    return parts[2] if len(parts) == 3 else call_id


def to_system_instruction(system: Any) -> str | None:
    """Anthropic sends a list of text blocks (with cache_control); Gemini takes
    one string. Cache control is dropped — Gemini caches implicitly."""
    if system is None:
        return None
    if isinstance(system, str):
        return system
    return "\n\n".join(
        block["text"] for block in system if isinstance(block, dict) and block.get("text")
    )


def to_function_declarations(tools: list[dict[str, Any]]) -> list[types.FunctionDeclaration]:
    declarations = []
    for tool in tools:
        schema = tool.get("input_schema") or {}
        properties = schema.get("properties") or {}
        declarations.append(
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool["description"],
                # A no-argument function must omit parameters entirely rather
                # than declare an empty object.
                parameters_json_schema=schema if properties else None,
            )
        )
    return declarations


def _field(block: Any, name: str, default: Any = None) -> Any:
    """Read a field from a block that may be a dataclass or a plain dict.

    Written as an explicit branch rather than `getattr(...) or block.get(...)`:
    a falsy-but-valid value — an empty tool input, an empty string — would send
    that expression down the wrong branch.
    """
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _parts_from_assistant(content: Any) -> list[types.Part]:
    if isinstance(content, str):
        return [types.Part.from_text(text=content)] if content.strip() else []

    parts: list[types.Part] = []
    for block in content:
        kind = _field(block, "type")
        if kind == "text":
            text = _field(block, "text", "") or ""
            if text.strip():
                parts.append(types.Part.from_text(text=text))
        elif kind == "tool_use":
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        name=_field(block, "name"),
                        args=dict(_field(block, "input", {}) or {}),
                    )
                )
            )
    return parts


def _parts_from_user(content: Any) -> list[types.Part]:
    if isinstance(content, str):
        return [types.Part.from_text(text=content)]

    parts: list[types.Part] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            name = decode_call_id(block.get("tool_use_id", ""))
            raw = block.get("content", "")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                payload = {"result": raw}
            if not isinstance(payload, dict):
                payload = {"result": payload}
            parts.append(types.Part.from_function_response(name=name, response=payload))
        elif block.get("type") == "text":
            parts.append(types.Part.from_text(text=block["text"]))
    return parts


def to_contents(messages: list[dict[str, Any]]) -> list[types.Content]:
    """Map the agent's message list onto Gemini contents.

    Gemini has no mid-conversation system role, so a system message becomes a
    marked user part. Consecutive same-role turns are merged, which both keeps
    Gemini happy and avoids a wall of one-part turns.
    """
    contents: list[types.Content] = []

    for message in messages:
        role = message["role"]
        if role == "assistant":
            gemini_role, parts = "model", _parts_from_assistant(message["content"])
        elif role == "system":
            gemini_role = "user"
            parts = [types.Part.from_text(text=f"{STATE_MARKER}\n\n{message['content']}")]
        else:
            gemini_role, parts = "user", _parts_from_user(message["content"])

        if not parts:
            continue
        if contents and contents[-1].role == gemini_role:
            contents[-1].parts.extend(parts)
        else:
            contents.append(types.Content(role=gemini_role, parts=parts))

    return contents


def from_response(response: Any) -> Response:
    """Turn a Gemini response into the block list the agent loop reads."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        # Blocked before any candidate was produced.
        return Response(content=[], stop_reason="refusal")

    candidate = candidates[0]
    finish = getattr(candidate, "finish_reason", None)
    finish_name = getattr(finish, "name", None) or (str(finish) if finish else "STOP")
    stop_reason = _FINISH_REASONS.get(finish_name, "end_turn")

    blocks: list[Any] = []
    parts = getattr(getattr(candidate, "content", None), "parts", None) or []
    for index, part in enumerate(parts):
        # Thinking parts carry text but are not the answer.
        if getattr(part, "thought", None):
            continue
        call = getattr(part, "function_call", None)
        if call is not None and getattr(call, "name", None):
            blocks.append(
                ToolUseBlock(
                    name=call.name,
                    input=dict(call.args or {}),
                    id=call.id or encode_call_id(call.name, index),
                )
            )
        elif getattr(part, "text", None):
            blocks.append(TextBlock(text=part.text))

    if any(isinstance(b, ToolUseBlock) for b in blocks):
        stop_reason = "tool_use"
    return Response(content=blocks, stop_reason=stop_reason)


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class _RateLimited(Exception):
    pass


class _Messages:
    def __init__(self, owner: "GeminiClient"):
        self.owner = owner

    def _config(self, kwargs: dict[str, Any], **extra: Any) -> types.GenerateContentConfig:
        tools = kwargs.get("tools")
        return types.GenerateContentConfig(
            system_instruction=to_system_instruction(kwargs.get("system")),
            max_output_tokens=kwargs.get("max_tokens"),
            tools=(
                [types.Tool(function_declarations=to_function_declarations(tools))]
                if tools
                else None
            ),
            # The loop drives tool calling itself; the SDK must not run a second
            # loop underneath it.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            **extra,
        )

    def _generate(self, model: str, contents: Any, config: Any) -> Any:
        """Free-tier requests-per-minute limits are low; back off rather than
        dropping the customer's turn."""
        delay = 2.0
        for attempt in range(self.owner.max_retries + 1):
            try:
                return self.owner.raw.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as exc:  # noqa: BLE001 - provider exception surface
                message = str(exc).lower()
                retryable = "resource_exhausted" in message or "429" in message or "rate" in message
                if not retryable or attempt == self.owner.max_retries:
                    raise
                time.sleep(delay)
                delay *= 2
        raise _RateLimited("exhausted retries")  # pragma: no cover

    def create(self, **kwargs: Any) -> Response:
        response = self._generate(
            kwargs.get("model") or self.owner.model,
            to_contents(kwargs.get("messages") or []),
            self._config(kwargs),
        )
        return from_response(response)

    def parse(self, **kwargs: Any) -> ParsedResponse:
        schema = kwargs["output_format"]
        response = self._generate(
            kwargs.get("model") or self.owner.model,
            to_contents(kwargs.get("messages") or []),
            self._config(
                kwargs,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )

        parsed = getattr(response, "parsed", None)
        if parsed is None:
            # Some models return the JSON as text without populating `parsed`.
            try:
                parsed = schema.model_validate_json(response.text or "{}")
            except Exception:  # noqa: BLE001 - a bad extraction must not kill the turn
                parsed = schema()
        return ParsedResponse(parsed_output=parsed, stop_reason="end_turn")


class _UnsupportedBeta:
    """Gemini has no server-side refusal fallbacks. The agent checks
    `supports_fallbacks` first; this exists so the path fails loudly if it
    is ever reached another way."""

    class messages:  # noqa: N801 - mirrors the Anthropic client shape
        @staticmethod
        def create(**_: Any) -> Any:
            raise RuntimeError("server-side fallbacks are not supported on this provider")


class GeminiClient:
    #: Read by the agent so it never attempts the Anthropic-only beta path.
    supports_fallbacks = False

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        max_retries: int = 3,
    ):
        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "No Gemini credentials. Get a free key at https://aistudio.google.com/apikey "
                "and set GEMINI_API_KEY."
            )
        self.raw = genai.Client(api_key=key)
        self.model = model or DEFAULT_MODEL
        self.max_retries = max_retries
        self.messages = _Messages(self)
        self.beta = _UnsupportedBeta()

    def available_models(self) -> list[str]:
        """Model names this key can actually reach that support generation."""
        names = []
        for model in self.raw.models.list():
            actions = getattr(model, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                names.append((model.name or "").removeprefix("models/"))
        return sorted(n for n in names if n)
