"""Loads the approved configuration.

Everything the agent is allowed to state as fact originates here or in the
engine that reads it. Loaded once and cached; call `reload()` after editing the
JSON (the `/demo-reset` admin command will use it).
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from .domain.models import Operator, Vehicle

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
FLEET_PATH = CONFIG_DIR / "fleet.json"
RULES_PATH = CONFIG_DIR / "rules.json"


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
    def messaging(self) -> dict[str, Any]:
        """How the agent behaves as a WhatsApp participant — typing, pacing,
        reactions. Absent in older config, so it defaults rather than raising."""
        return self._data.get("messaging", {})

    def minimum_age_for(self, category: str) -> int:
        return int(self._data["driver_requirements"]["minimum_age_by_category"][category])

    def insurance_excess_for(self, category: str) -> Decimal:
        return Decimal(str(self._data["insurance"]["cdw_excess_by_category"][category]))

    def as_dict(self) -> dict[str, Any]:
        return self._data


@lru_cache(maxsize=1)
def load_rules() -> Rules:
    return Rules(_load_json(RULES_PATH))


@lru_cache(maxsize=1)
def load_fleet() -> tuple[Operator, tuple[Vehicle, ...]]:
    raw = _load_json(FLEET_PATH)
    operator = Operator.model_validate(raw["operator"])
    vehicles = tuple(Vehicle.model_validate(v) for v in raw["vehicles"])
    ids = [v.id for v in vehicles]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate vehicle ids in fleet.json")
    return operator, vehicles


def reload() -> None:
    """Drop the caches so edited config takes effect without a restart."""
    load_rules.cache_clear()
    load_fleet.cache_clear()
