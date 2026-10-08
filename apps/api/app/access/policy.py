"""Deterministic access policy. ML never takes part in these decisions (CLAUDE.md rule 3)."""

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.db.enums import AccessAction, ResourceType, Role

POLICY_PATH = Path(__file__).with_name("policy.yaml")


class Requirement(StrEnum):
    NONE = "none"
    EXPLANATION_OR_REVIEW = "explanation_or_review"
    BREAK_GLASS_ONLY = "break_glass_only"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    requirement: Requirement
    allowed: bool


@dataclass(frozen=True)
class Policy:
    version: str
    sha256: str
    yaml_text: str
    rules: dict[tuple[str, str, str], Requirement]

    def requirement(self, role: Role, resource: ResourceType, action: AccessAction) -> Requirement:
        return self.rules.get((role.value, resource.value, action.value), Requirement.DENY)

    def decide(
        self,
        role: Role,
        resource: ResourceType,
        action: AccessAction,
        break_glass: bool = False,
    ) -> Decision:
        req = self.requirement(role, resource, action)
        if req == Requirement.DENY:
            return Decision(req, False)
        if req == Requirement.BREAK_GLASS_ONLY:
            return Decision(req, break_glass)
        return Decision(req, True)


def parse_policy(text: str) -> Policy:
    data: dict[str, Any] = yaml.safe_load(text)
    rules: dict[tuple[str, str, str], Requirement] = {}
    for role, resources in data["rules"].items():
        Role(role)
        for resource, actions in resources.items():
            ResourceType(resource)
            for action, requirement in actions.items():
                AccessAction(action)
                rules[(role, resource, action)] = Requirement(requirement)
    return Policy(
        version=str(data["version"]),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        yaml_text=text,
        rules=rules,
    )


@lru_cache
def get_policy() -> Policy:
    return parse_policy(POLICY_PATH.read_text(encoding="utf-8"))
