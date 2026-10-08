"""The product's access policy, read from the same file the API uses, so simulated allow and deny
decisions and policy_version match the product exactly."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

POLICY_PATH = (
    Path(__file__).resolve().parents[3] / "apps" / "api" / "app" / "access" / "policy.yaml"
)


@dataclass(frozen=True)
class Policy:
    sha256: str
    rules: dict[tuple[str, str, str], str]

    def allows(self, role: str, resource: str, action: str, break_glass: bool = False) -> bool:
        requirement = self.rules.get((role, resource, action), "deny")
        if requirement == "deny":
            return False
        if requirement == "break_glass_only":
            return break_glass
        return True


def load_policy(path: Path = POLICY_PATH) -> Policy:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    rules = {
        (role, resource, action): str(requirement)
        for role, resources in data["rules"].items()
        for resource, actions in resources.items()
        for action, requirement in actions.items()
    }
    return Policy(hashlib.sha256(text.encode("utf-8")).hexdigest(), rules)
