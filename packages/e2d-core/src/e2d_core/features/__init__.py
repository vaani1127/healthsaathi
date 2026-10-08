"""Residual features of accesses (SPEC 5.4). Feature code never reads simulator labels."""

from e2d_core.features.behaviour import FEATURES as BEHAVIOUR_FEATURES
from e2d_core.features.behaviour import behaviour_features

__all__ = ["BEHAVIOUR_FEATURES", "behaviour_features"]
