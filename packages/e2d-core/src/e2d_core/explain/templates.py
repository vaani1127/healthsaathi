import re
from dataclasses import dataclass
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
    params: tuple[tuple[str, Any], ...] = ()

    def applies_to(self, role: str, resource: str) -> bool:
        return role in self.roles and resource in self.resources

    def param(self, name: str, default: Any = None) -> Any:
        return dict(self.params).get(name, default)


@dataclass(frozen=True, slots=True)
class TemplateConfig:
    templates: dict[str, TemplateSpec]
    resource_groups: dict[str, frozenset[str]]

    def get(self, code: str) -> TemplateSpec | None:
        return self.templates.get(code)


_KNOWN_KEYS = {"roles", "resources", "weight", "before", "after", "tau"}


def parse_config(data: dict[str, Any]) -> TemplateConfig:
    groups = {name: frozenset(items) for name, items in data.get("resource_groups", {}).items()}
    templates = {}
    for code, raw in data["templates"].items():
        res = raw["resources"]
        resource_set = groups[res] if isinstance(res, str) and res in groups else frozenset(res)
        weight = float(raw["weight"])
        if not 0 < weight <= 1:
            raise ValueError(f"{code}: weight must be in (0, 1]")
        templates[code] = TemplateSpec(
            code=code,
            roles=frozenset(raw["roles"]),
            resources=resource_set,
            weight=weight,
            before=parse_duration(raw.get("before", 0)),
            after=parse_duration(raw.get("after", 0)),
            tau=parse_duration(raw.get("tau", "1h")),
            params=tuple(sorted((k, v) for k, v in raw.items() if k not in _KNOWN_KEYS)),
        )
    return TemplateConfig(templates=templates, resource_groups=groups)


@lru_cache
def default_config() -> TemplateConfig:
    text = resources.files("e2d_core.explain").joinpath("templates.yaml").read_text("utf-8")
    return parse_config(yaml.safe_load(text))
