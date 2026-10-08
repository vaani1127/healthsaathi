from datetime import date

import numpy as np
import polars as pl
import pytest

from e2d_core.detect import (
    DEFAULT_BUDGET,
    SCORERS,
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

COLUMNS = ("a", "b")


def normal(n: int, seed: int = 0, shift: float = 0.0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    return pl.DataFrame({"a": rng.normal(shift, 1, n), "b": rng.normal(shift, 1, n)})


def test_gate() -> None:
    features = pl.DataFrame(
        {
            "sigma": [0.9, 0.2, 0.9, 0.9],
            "flags_any": [0, 0, 1, 0],
            "break_glass": [0, 0, 0, 1],
        }
    )
    assert gate(features).to_list() == [False, True, True, True]
    assert gate(features, theta=0.1).to_list() == [False, False, True, True]


def test_rules_score() -> None:
    features = pl.DataFrame(
        {
            "sigma": [1.0, 0.0],
            "flags_any": [0, 1],
            "off_shift": [0, 1],
            "new_device": [0, 1],
            "exports_1h": [0, 12],
            "patients_1h": [1, 25],
            "path_len": [1, 4],
            "shares_surname": [0, 1],
            "shares_address": [0, 0],
        }
    )
    assert rules_score(features).tolist() == [0.0, 6.0]


@pytest.mark.parametrize("scorer", SCORERS)
def test_scorers_rank_outliers_higher(scorer: str) -> None:
    bundle = fit(normal(800), COLUMNS, scorer, seed=1)
    usual = bundle.score(normal(50, seed=2)).mean()
    odd = bundle.score(normal(50, seed=3, shift=6)).mean()
    assert odd > usual
    assert bundle.trained_rows == 800 and bundle.columns == COLUMNS


def test_model_files_and_versions() -> None:
    bundle = fit(normal(200), COLUMNS)
    data = dumps(bundle)
    version = model_version(data)
    assert len(version) == 64 and version == model_version(data)
    again = loads(data)
    assert np.allclose(again.score(normal(5)), bundle.score(normal(5)))
    with pytest.raises(TypeError):
        loads(dumps({"not": "a model"}))  # type: ignore[arg-type]


def test_fit_errors() -> None:
    with pytest.raises(ValueError, match="no rows"):
        fit(normal(0), COLUMNS)
    with pytest.raises(ValueError, match="unknown scorer"):
        fit(normal(10), COLUMNS, "magic")


def test_top_features_and_calibration() -> None:
    bundle = fit(normal(500), COLUMNS)
    top = bundle.top_features({"a": 0.0, "b": 9.0}, k=1)
    assert top[0]["feature"] == "b" and top[0]["z"] > 3
    threshold = calibrate(bundle, normal(500), days=10, budget=5)
    scores = bundle.score(normal(500))
    assert threshold == bundle.threshold
    # 500 rows over 10 days and 5 alerts a day: about 50 of the rows score above it.
    assert 40 <= (scores > threshold).sum() <= 60
    assert calibrate(bundle, normal(1), days=1, budget=5) == bundle.threshold


def test_per_clinic_models_with_global_fallback() -> None:
    models, fallback = fit_per_clinic(
        {"big": normal(600), "small": normal(20)}, COLUMNS, min_rows=500
    )
    assert set(models) == {"big"} and models["big"].clinic_id == "big"
    assert fallback is not None and fallback.trained_rows == 620 and fallback.clinic_id is None
    assert fit_per_clinic({}, COLUMNS) == ({}, None)


def test_budget_ranks_per_clinic_day_and_skips_break_glass() -> None:
    day1, day2 = date(2026, 3, 2), date(2026, 3, 3)
    scored = pl.DataFrame(
        {
            "access_event_id": ["a", "b", "c", "d", "e", "f", "g"],
            "clinic_id": ["x", "x", "x", "x", "x", "y", "x"],
            "day": [day1, day1, day1, day1, day2, day1, day1],
            "score": [0.9, 0.5, 0.7, 2.0, 0.1, 0.3, 0.0],
            "gated": [True, True, True, True, True, True, True],
            "break_glass": [0, 0, 0, 1, 0, 0, 0],
        }
    )
    out = budget(scored, b=2)
    rank = dict(zip(out["access_event_id"], out["rank_in_day"], strict=True))
    alert = dict(zip(out["access_event_id"], out["alert"], strict=True))
    assert (rank["a"], rank["c"], rank["b"]) == (1, 2, 3)
    assert rank["d"] is None and not alert["d"], "break-glass goes to its own queue"
    assert rank["g"] is None, "a zero score is not an alert"
    assert (rank["e"], rank["f"]) == (1, 1), "each clinic and day has its own budget"
    assert [k for k, v in alert.items() if v] == ["a", "c", "e", "f"]
    assert DEFAULT_BUDGET == 5
