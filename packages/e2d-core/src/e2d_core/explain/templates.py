import re
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: str | int | float) -> timedelta:
    if isinstance(value, int | float):
        return timedelta(seconds=float(value))
    match = _DURATION.match(value.strip())
    if not match:
        raise ValueError(f"bad duration: {value!r}")
    return timedelta(**{_UNITS[match.group(2)]: float(match.group(1))})


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    code: str
    roles: frozenset[str]
    resources: frozenset[str]
    weight: float
    before: timedelta = timedelta(0)
    after: timedelta = timedelta(0)
    tau: timedelta = timedelta(hours=1)
    lookback: timedelta = timedelta(0)
    role_resources: tuple[tuple[str, frozenset[str]], ...] = ()

    def applies_to(self, role: str, resource: str) -> bool:
        if role not in self.roles:
            return False
        per_role = dict(self.role_resources)
        allowed = per_role.get(role, self.resources)
        return resource in allowed


@dataclass(frozen=True, slots=True)
class ForgeryConfig:
    self_created_recent: timedelta = timedelta(minutes=60)
    no_progress_after: timedelta = timedelta(hours=24)
    reception_norm: float = 0.8
    self_creation_high: float = 3.0


@dataclass(frozen=True, slots=True)
class TemplateConfig:
    templates: dict[str, TemplateSpec]
    resource_groups: dict[str, frozenset[str]]
    forgery: ForgeryConfig = field(default_factory=ForgeryConfig)
    theta: float = 0.5
    min_strength: float = 0.001

    def get(self, code: str) -> TemplateSpec | None:
        return self.templates.get(code)


def _resources(value: Any, groups: dict[str, frozenset[str]]) -> frozenset[str]:
    if isinstance(value, str):
        if value not in groups:
            raise ValueError(f"unknown resource group {value!r}")
        return groups[value]
    return frozenset(value)


def parse_config(data: dict[str, Any]) -> TemplateConfig:
    groups = {name: frozenset(items) for name, items in data.get("resource_groups", {}).items()}
    templates = {}
    for code, raw in data["templates"].items():
        weight = float(raw["weight"])
        if not 0 < weight <= 1:
            raise ValueError(f"{code}: weight must be in (0, 1]")
        templates[code] = TemplateSpec(
            code=code,
            roles=frozenset(raw["roles"]),
            resources=_resources(raw["resources"], groups),
            weight=weight,
            before=parse_duration(raw.get("before", 0)),
            after=parse_duration(raw.get("after", 0)),
            tau=parse_duration(raw.get("tau", "1h")),
            lookback=parse_duration(raw.get("lookback", 0)),
            role_resources=tuple(
                sorted(
                    (role, _resources(res, groups))
                    for role, res in raw.get("role_resources", {}).items()
                )
            ),
        )
    raw_forgery = data.get("forgery", {})
    forgery = ForgeryConfig(
        self_created_recent=parse_duration(raw_forgery.get("self_created_recent", "60m")),
        no_progress_after=parse_duration(raw_forgery.get("no_progress_after", "24h")),
        reception_norm=float(raw_forgery.get("reception_norm", 0.8)),
        self_creation_high=float(raw_forgery.get("self_creation_high", 3.0)),
    )
    gate = data.get("gate", {})
    return TemplateConfig(
        templates=templates,
        resource_groups=groups,
        forgery=forgery,
        theta=float(gate.get("theta", 0.5)),
        min_strength=float(gate.get("min_strength", 0.001)),
    )


@lru_cache
def default_config() -> TemplateConfig:
    text = resources.files("e2d_core.explain").joinpath("templates.yaml").read_text("utf-8")
    return parse_config(yaml.safe_load(text))
