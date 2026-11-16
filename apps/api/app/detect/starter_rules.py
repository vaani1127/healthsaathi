"""Starter rules: how the product ranks accesses before it has a fitted model.

This is the product's own fallback, not the B0 rules baseline of the experiments (which uses raw
behaviour only and is defined in experiments/). The rules, their points and the threshold live in
e2d-core (`e2d_core.detect.RULES`), where they are still labelled B0; that label is stale and is
listed in docs/APP_GAPS.md. The app uses them only through this module.
"""

import json

import numpy as np
import polars as pl

from e2d_core.detect import RULES, RULES_THRESHOLD, model_version, rules_score

NAME = "starter_rules"
STARTER_RULES = RULES
THRESHOLD = RULES_THRESHOLD
# The same bytes as before the rename, so alerts raised earlier keep a matching version.
VERSION = model_version(json.dumps([RULES, RULES_THRESHOLD]).encode())


def score(features: pl.DataFrame) -> np.ndarray:
    """One minus the explanation strength, plus points for each starter rule that fires."""
    return rules_score(features)


def fired(values: dict[str, float]) -> list[dict[str, float | str]]:
    """The rules that fired for one access, in the shape of ModelBundle.top_features."""
    return [
        {"feature": name, "value": values[name], "z": 0.0}
        for name, at_least, _ in STARTER_RULES
        if values[name] >= at_least
    ]
