"""Agent configuration.

Everything is overridable by environment variable so a demo can be tuned without
touching code — including which model provider runs the conversation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Model to use when none is configured, per provider. A model name is only
#: meaningful to its own provider, so this is resolved at use time rather than
#: baked into a single `model` default.
PROVIDER_DEFAULT_MODELS = {
    "gemini": os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
    "anthropic": "claude-opus-5",
}

DEFAULT_PROVIDER = os.getenv("RENTAL_AGENT_PROVIDER", "gemini")


@dataclass(frozen=True)
class AgentSettings:
    #: "gemini" (free tier) or "anthropic". The engine, tools, persistence and
    #: state are provider-agnostic; only the adapter changes.
    provider: str = DEFAULT_PROVIDER

    #: None means "this provider's default model".
    model: str | None = os.getenv("RENTAL_AGENT_MODEL") or None
    extraction_model: str | None = os.getenv("RENTAL_AGENT_EXTRACTION_MODEL") or None

    #: WhatsApp is latency-sensitive, so below the usual default. Honoured by
    #: the Anthropic adapter; Gemini has no equivalent knob and ignores it.
    effort: str = os.getenv("RENTAL_AGENT_EFFORT", "medium")
    extraction_effort: str = os.getenv("RENTAL_AGENT_EXTRACTION_EFFORT", "low")

    #: Replies are a few lines, but reasoning counts against the output budget,
    #: so this needs headroom despite the short visible output.
    max_tokens: int = int(os.getenv("RENTAL_AGENT_MAX_TOKENS", "8000"))
    extraction_max_tokens: int = int(os.getenv("RENTAL_AGENT_EXTRACTION_MAX_TOKENS", "4000"))

    #: Hard stop on the tool loop so a confused turn cannot spin.
    max_tool_iterations: int = int(os.getenv("RENTAL_AGENT_MAX_TOOL_ITERATIONS", "8"))

    #: Turn off the extraction pass to run the agent on tools alone (useful for
    #: isolating which layer caused a bad turn).
    extraction_enabled: bool = os.getenv("RENTAL_AGENT_EXTRACTION", "1") != "0"

    def resolved_model(self) -> str:
        return self.model or PROVIDER_DEFAULT_MODELS.get(self.provider, "")

    def resolved_extraction_model(self) -> str:
        return self.extraction_model or self.resolved_model()


#: Anthropic-only: re-runs a policy-declined request on another model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
