"""Model providers.

The agent loop depends on a small duck-typed surface, not on any SDK:

    client.messages.create(model=, max_tokens=, system=, tools=, messages=, ...)
        -> object with .content (blocks) and .stop_reason
    client.messages.parse(..., output_format=PydanticModel)
        -> object with .parsed_output and .stop_reason
    client.supports_fallbacks -> bool

Blocks carry `.type` plus `.text`, or `.name` / `.input` / `.id`. Anything that
presents that surface can drive the agent — which is also why the whole loop is
testable against a scripted fake with no network at all.
"""

from __future__ import annotations

from typing import Any


def build_client(provider: str, *, model: str | None = None) -> Any:
    """Construct the client for a provider.

    Imports are local so a missing SDK for one provider never blocks the other.
    """
    if provider == "gemini":
        from .gemini import GeminiClient

        return GeminiClient(model=model)

    if provider == "anthropic":
        import anthropic

        # Credentials resolve from ANTHROPIC_API_KEY or an `ant auth login`
        # profile; the zero-argument constructor handles both.
        return anthropic.Anthropic()

    raise ValueError(f"Unknown provider {provider!r}. Use 'gemini' or 'anthropic'.")
