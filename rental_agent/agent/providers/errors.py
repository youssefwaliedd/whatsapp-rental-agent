"""Provider-neutral errors.

Kept in its own module so the agent loop can catch a transient provider failure
without importing any particular SDK.
"""

from __future__ import annotations


class ProviderUnavailable(Exception):
    """The provider could not serve the request after retries (rate limit,
    capacity, timeout). Distinct from a bad request or a refusal."""
