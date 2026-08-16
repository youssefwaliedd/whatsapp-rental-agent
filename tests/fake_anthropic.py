"""A scriptable stand-in for the Anthropic client.

The agent must be testable without an API key and without network flake — every
behaviour that matters (tool dispatch, refusal handling, loop limits, the
additive state merge) is deterministic given a scripted model response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rental_agent.agent.extraction import Extraction


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = "toolu_test"
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list[Any]
    stop_reason: str = "end_turn"


def says(text: str) -> FakeResponse:
    return FakeResponse(content=[TextBlock(text=text)])


def calls(name: str, **args: Any) -> FakeResponse:
    return FakeResponse(
        content=[ToolUseBlock(name=name, input=args, id=f"toolu_{name}")],
        stop_reason="tool_use",
    )


def refuses() -> FakeResponse:
    return FakeResponse(content=[], stop_reason="refusal")


@dataclass
class ParsedResponse:
    parsed_output: Extraction
    stop_reason: str = "end_turn"


class FakeMessages:
    def __init__(self, owner: "FakeClient"):
        self.owner = owner

    def create(self, **kwargs: Any) -> FakeResponse:
        self.owner.requests.append(kwargs)
        if not self.owner.script:
            raise AssertionError("FakeClient ran out of scripted responses")
        return self.owner.script.pop(0)

    def parse(self, **kwargs: Any) -> ParsedResponse:
        self.owner.extraction_requests.append(kwargs)
        if self.owner.extractions:
            return ParsedResponse(parsed_output=self.owner.extractions.pop(0))
        return ParsedResponse(parsed_output=Extraction())


class FakeBetaMessages(FakeMessages):
    def create(self, **kwargs: Any) -> FakeResponse:
        if not self.owner.supports_fallbacks:
            raise RuntimeError("fallbacks beta is not enabled for this account")
        self.owner.beta_calls += 1
        kwargs.pop("betas", None)
        kwargs.pop("fallbacks", None)
        return super().create(**kwargs)


class FakeBeta:
    def __init__(self, owner: "FakeClient"):
        self.messages = FakeBetaMessages(owner)


class FakeClient:
    """Replays `script` for conversational turns and `extractions` for the
    extraction pass, recording every request for assertions."""

    def __init__(
        self,
        script: list[FakeResponse] | None = None,
        extractions: list[Extraction] | None = None,
        supports_fallbacks: bool = True,
    ):
        self.script: list[FakeResponse] = list(script or [])
        self.extractions: list[Extraction] = list(extractions or [])
        self.supports_fallbacks = supports_fallbacks
        self.requests: list[dict[str, Any]] = []
        self.extraction_requests: list[dict[str, Any]] = []
        self.beta_calls = 0
        self.messages = FakeMessages(self)
        self.beta = FakeBeta(self)

    # -- assertions helpers ---------------------------------------------

    @property
    def last_request(self) -> dict[str, Any]:
        return self.requests[-1]

    def system_texts(self) -> list[str]:
        """Every system-role message body sent across all requests."""
        out = []
        for request in self.requests:
            for message in request.get("messages", []):
                if message.get("role") == "system":
                    out.append(message["content"])
        return out
