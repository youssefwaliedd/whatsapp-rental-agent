"""Provider-neutral errors.

Kept in its own module so the agent loop can catch a transient provider failure
without importing any particular SDK.
"""

from __future__ import annotations

import re


class ProviderUnavailable(Exception):
    """The provider could not serve the request after retries (rate limit,
    capacity, timeout). Distinct from a bad request or a refusal."""

    retry_after: float | None = None


def safe_provider_error(exc) -> str:
    """A useful diagnostic without provider bodies, prompts or credentials."""
    message = f'{type(exc).__name__}: {exc}'.lower()
    code = re.search(r'\b(400|401|403|404|429|500|502|503|504)\b', message)
    if 'perday' in message or 'daily_quota' in message:
        kind = 'daily_quota'
    elif '429' in message or 'rate_limit' in message or 'resource_exhausted' in message:
        kind = 'rate_limit'
    elif '503' in message or 'overload' in message:
        kind = 'overloaded'
    elif any(word in message for word in ('timeout', 'timed out', 'deadline')):
        kind = 'timeout_or_deadline'
    elif any(word in message for word in ('connection', 'connecterror', 'network', 'getaddrinfo', 'ssl')):
        kind = 'network_error'
    elif code and code.group() in {'400', '401', '403', '404'}:
        kind = 'configuration_error'
    else:
        kind = 'provider_error'
    return kind + (f' (HTTP {code.group()})' if code else '')
