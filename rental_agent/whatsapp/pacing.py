"""How a reply arrives, as opposed to what it says.

A reply that is correct but lands as three messages in the same instant reads as
a machine. A real salesperson answers quickly, then the detail follows at the
speed they can type it. That difference is invisible in a transcript and obvious
on a phone, which is why it lives in code rather than in the prompt.

Two rules shape everything here:

**The first message is never delayed.** A customer waiting on an answer should
get one immediately; pacing applies only to what follows. Delaying the opener
would spend the response-time budget on theatre.

**Delay is composing time, not a fixed beat.** A long paragraph takes longer to
type than a short one, so the gap is proportional to length and clamped at both
ends — below the floor it looks instant, above the ceiling it looks broken.

The sleeper is injected, so the whole schedule is asserted in tests without a
test suite that actually waits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..config import load_rules

#: Used when `rules.json` carries no `messaging.pacing` block.
DEFAULTS = {
    "enabled": True,
    "seconds_per_character": 0.018,
    "min_seconds": 1.0,
    "max_seconds": 4.5,
}


@dataclass
class Pacing:
    enabled: bool = True
    seconds_per_character: float = 0.018
    min_seconds: float = 1.0
    max_seconds: float = 4.5

    @classmethod
    def from_rules(cls, rules=None) -> "Pacing":
        config = {**DEFAULTS, **(rules or load_rules()).messaging.get("pacing", {})}
        return cls(
            enabled=bool(config["enabled"]),
            seconds_per_character=float(config["seconds_per_character"]),
            min_seconds=float(config["min_seconds"]),
            max_seconds=float(config["max_seconds"]),
        )

    def compose_seconds(self, text: str) -> float:
        """How long a person would plausibly take to type this.

        Clamped hard at both ends: a two-word confirmation still deserves a
        beat, and a long quote must not leave the customer staring at a typing
        indicator wondering whether anything is coming.
        """
        if not self.enabled:
            return 0.0
        raw = len(text or "") * self.seconds_per_character
        return max(self.min_seconds, min(self.max_seconds, raw))

    def schedule(self, parts: list[str]) -> list[float]:
        """The delay to wait *before* each part. The first is always zero."""
        return [0.0] + [self.compose_seconds(part) for part in parts[1:]]


class Pacer:
    """Applies a schedule. Holds the only `sleep` call in the transport."""

    def __init__(self, pacing: Pacing | None = None, sleep: Callable[[float], None] | None = None):
        self.pacing = pacing or Pacing.from_rules()
        #: Injected so tests assert the schedule instead of living through it.
        self._sleep = sleep if sleep is not None else time.sleep

    def wait(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)

    def before_part(self, index: int, text: str) -> float:
        """Pause before part `index`, and report how long it waited."""
        if index == 0:
            return 0.0
        seconds = self.pacing.compose_seconds(text)
        self.wait(seconds)
        return seconds
