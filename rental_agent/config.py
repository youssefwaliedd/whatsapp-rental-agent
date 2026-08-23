"""Loads the approved configuration.

Everything the agent is allowed to state as fact originates here or in the
engine that reads it. Loaded once and cached; call `reload()` after editing the
JSON (the `/demo-reset` admin command will use it).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from .domain.models import Operator, Vehicle

#: Where the approved configuration lives. Overridable so the test suite can run
#: against a fixed, fictional fleet rather than whatever an operator's live
#: pricing happens to be today — otherwise every rate change on their website
#: breaks the tests, and the tests stop meaning anything.
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def config_dir() -> Path:
    """Resolved at call time, not import time, so a test can set it late."""
    override = os.getenv("RENTAL_AGENT_CONFIG_DIR")
    return Path(override) if override else DEFAULT_CONFIG_DIR


#: Kept for callers that want the paths directly. Both follow the override.
CONFIG_DIR = DEFAULT_CONFIG_DIR


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        # parse_float=Decimal keeps money exact from the moment it leaves disk.
        return json.load(fh, parse_float=Decimal)


class Rules:
    """Thin typed accessor over rules.json.

    Deliberately not a Pydantic model: the file is meant to be edited freely by
    whoever configures a demo, and an unknown key should not crash the engine.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def version(self) -> str:
        return self._data["version"]

    @property
    def currency(self) -> str:
        return self._data["currency"]

    @property
    def timezone(self) -> str:
        return self._data["timezone"]

    @property
    def vat_percent(self) -> Decimal:
        return Decimal(str(self._data["vat_percent"]))

    @property
    def rental_period(self) -> dict[str, Any]:
        return self._data["rental_period"]

    @property
    def delivery(self) -> dict[str, Any]:
        return self._data["delivery"]

    @property
    def discount_policy(self) -> dict[str, Any]:
        return self._data["discount_policy"]

    @property
    def insurance(self) -> dict[str, Any]:
        return self._data["insurance"]

    @property
    def driver_requirements(self) -> dict[str, Any]:
        return self._data["driver_requirements"]

    @property
    def demo_disclosure(self) -> dict[str, Any]:
        return self._data["demo_disclosure"]

    @property
    def escalation_triggers(self) -> list[str]:
        return self._data["escalation_triggers"]

    @property
    def human_in_the_loop(self) -> dict[str, Any]:
        """What happens after a case is handed to a person."""
        return self._data.get("human_in_the_loop", {})

    @property
    def messaging(self) -> dict[str, Any]:
        """How the agent behaves as a WhatsApp participant — typing, pacing,
        reactions. Absent in older config, so it defaults rather than raising."""
        return self._data.get("messaging", {})

    def minimum_age_for(self, category: str) -> int:
        return int(self._data["driver_requirements"]["minimum_age_by_category"][category])

    def insurance_excess_for(self, category: str) -> Decimal:
        return Decimal(str(self._data["insurance"]["cdw_excess_by_category"][category]))

    @property
    def excess_is_minimum(self) -> bool:
        """Whether the excess above is a floor rather than the amount.

        Delta publishes it as "from AED 5,000, depending on the vehicle", so the
        figure is the bottom of a range the insurer sets. Stating it flat would
        understate what a customer could owe after an accident."""
        return bool(self._data["insurance"].get("cdw_excess_is_minimum", False))

    def as_dict(self) -> dict[str, Any]:
        return self._data


@lru_cache(maxsize=1)
def load_rules() -> Rules:
    return Rules(_load_json(config_dir() / "rules.json"))


@lru_cache(maxsize=1)
def load_fleet() -> tuple[Operator, tuple[Vehicle, ...]]:
    raw = _load_json(config_dir() / "fleet.json")
    operator = Operator.model_validate(raw["operator"])
    vehicles = tuple(Vehicle.model_validate(v) for v in raw["vehicles"])
    ids = [v.id for v in vehicles]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate vehicle ids in fleet.json")
    return operator, vehicles


@lru_cache(maxsize=1)
def load_playbook() -> str:
    """How the operator sells, in their own patterns. Empty when absent.

    Held to the same rule as the policy documents: it may not state a figure. A
    number in here is one the model reads every single turn as guidance, which
    is the most reliable way there is to teach it to repeat a price nobody
    calculated. Sequence and wording here; amounts in this file's siblings.
    """
    path = config_dir() / "playbook.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")

    from .knowledge.retrieval import PolicyContainsFigures, _forbidden_figures

    figures = _forbidden_figures(text)
    if figures:
        raise PolicyContainsFigures(
            f"playbook.md states {figures[0]!r}. The playbook is read on every turn, so a "
            "figure in it is one the agent will eventually quote without any tool having "
            "produced it. Amounts belong in rules.json."
        )
    return text.strip()


def reload() -> None:
    """Drop the caches so edited config takes effect without a restart."""
    load_rules.cache_clear()
    load_fleet.cache_clear()
    load_playbook.cache_clear()
