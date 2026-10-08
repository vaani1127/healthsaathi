"""Explanation features of an access (SPEC 5.4): sigma, template one-hot and forgery flags."""

from collections.abc import Iterable
from typing import Any

import polars as pl

from e2d_core.explain import Explanation, default_config
from e2d_core.explain.forgery import FLAG_NAMES

TEMPLATES = tuple(sorted(default_config().templates))
FLAG_COLUMNS = tuple(f"flag_{name}" for name in FLAG_NAMES)

FEATURES = (
    "sigma",
    *(f"tpl_{code}" for code in TEMPLATES),
    "tpl_none",
    "break_glass",
)
FORGERY_FEATURES = (*FLAG_COLUMNS, "flags_any", "flags_count")

SCHEMA: dict[str, Any] = {
    "access_event_id": pl.String,
    "template_code": pl.String,
    "strength": pl.Float64,
    "break_glass": pl.Boolean,
    **{c: pl.Boolean for c in FLAG_COLUMNS},
}


def explanation_rows(
    results: Iterable[tuple[str, Explanation, bool]],
) -> pl.DataFrame:
    """A frame from (access_event_id, explanation, break_glass) triples. A flag that could not be
    decided yet (None) stays null."""
    rows = [
        {
            "access_event_id": event_id,
            "template_code": result.template_code,
            "strength": result.strength,
            "break_glass": break_glass,
            **{f"flag_{n}": result.forgery_flags.get(n) for n in FLAG_NAMES},
        }
        for event_id, result, break_glass in results
    ]
    return pl.DataFrame(rows, schema=SCHEMA, orient="row")


def flags_from_json(flags: dict[str, Any] | None) -> dict[str, bool | None]:
    """Stored access_explanations.forgery_flags to flag columns (values other than booleans,
    such as the self-creation rate, are not flags)."""
    flags = flags or {}
    return {
        f"flag_{n}": flags.get(n) if isinstance(flags.get(n), bool) else None for n in FLAG_NAMES
    }


def explanation_features(explanations: pl.DataFrame) -> pl.DataFrame:
    """access_event_id plus FEATURES and FORGERY_FEATURES, in the order of `explanations`."""
    missing = set(SCHEMA) - set(explanations.columns)
    if missing:
        raise ValueError(f"explanations are missing columns {sorted(missing)}")
    code = pl.col("template_code")
    flags = [pl.col(c).fill_null(False).cast(pl.Int8).alias(c) for c in FLAG_COLUMNS]
    return explanations.select(
        "access_event_id",
        pl.col("strength").fill_null(0.0).alias("sigma"),
        *((code == t).fill_null(False).cast(pl.Int8).alias(f"tpl_{t}") for t in TEMPLATES),
        code.is_null().cast(pl.Int8).alias("tpl_none"),
        pl.col("break_glass").fill_null(False).cast(pl.Int8),
        *flags,
    ).with_columns(
        pl.max_horizontal(FLAG_COLUMNS).alias("flags_any"),
        pl.sum_horizontal(FLAG_COLUMNS).alias("flags_count"),
    )
