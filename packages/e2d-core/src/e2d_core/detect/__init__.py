"""Detection (SPEC 5.5): gate, scorers, per-clinic fitting and budgeted alerts."""

from e2d_core.detect.scoring import (
    DEFAULT_BUDGET,
    DEFAULT_SCORER,
    MIN_CLINIC_ROWS,
    RULES,
    RULES_THRESHOLD,
    SCORERS,
    ModelBundle,
    budget,
    calibrate,
    dumps,
    fit,
    fit_per_clinic,
    gate,
    loads,
    model_version,
    rules_score,
)

__all__ = [
    "DEFAULT_BUDGET",
    "DEFAULT_SCORER",
    "MIN_CLINIC_ROWS",
    "RULES",
    "RULES_THRESHOLD",
    "SCORERS",
    "ModelBundle",
    "budget",
    "calibrate",
    "dumps",
    "fit",
    "fit_per_clinic",
    "gate",
    "loads",
    "model_version",
    "rules_score",
]
