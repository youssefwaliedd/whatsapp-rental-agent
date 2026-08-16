"""Agent configuration.

Everything here is overridable by environment variable so a demo can be tuned
without touching code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: The model that talks to customers.
DEFAULT_MODEL = "claude-opus-5"


@dataclass(frozen=True)
class AgentSettings:
    model: str = os.getenv("RENTAL_AGENT_MODEL", DEFAULT_MODEL)

    #: The extraction pass is a narrow, scoped task. It runs on the same model by
    #: default; a smaller one is a reasonable cost tuning if a demo needs it.
    extraction_model: str = os.getenv("RENTAL_AGENT_EXTRACTION_MODEL", DEFAULT_MODEL)

    #: WhatsApp is a latency-sensitive surface and this model is unusually strong
    #: at lower effort, so `medium` rather than the API default of `high`.
    effort: str = os.getenv("RENTAL_AGENT_EFFORT", "medium")
    extraction_effort: str = os.getenv("RENTAL_AGENT_EXTRACTION_EFFORT", "low")

    #: Replies are a few lines, but thinking counts against max_tokens, so this
    #: needs real headroom despite the short visible output.
    max_tokens: int = int(os.getenv("RENTAL_AGENT_MAX_TOKENS", "8000"))
    extraction_max_tokens: int = int(os.getenv("RENTAL_AGENT_EXTRACTION_MAX_TOKENS", "4000"))

    #: Safety classifiers can decline a request. Server-side fallbacks re-run it
    #: on another model rather than leaving the customer with nothing.
    use_fallbacks: bool = os.getenv("RENTAL_AGENT_FALLBACKS", "1") != "0"

    #: Hard stop on the tool loop so a confused turn cannot spin.
    max_tool_iterations: int = int(os.getenv("RENTAL_AGENT_MAX_TOOL_ITERATIONS", "8"))

    #: Turn off the extraction pass to run the agent on tools alone (useful for
    #: isolating which layer caused a bad turn).
    extraction_enabled: bool = os.getenv("RENTAL_AGENT_EXTRACTION", "1") != "0"


FALLBACK_BETA = "server-side-fallback-2026-07-01"
