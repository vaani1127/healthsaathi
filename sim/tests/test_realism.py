from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from saathibench.config import AttackConfig, RealismConfig, load_config, parse_realism
from saathibench.realism import measure
from saathibench.run import config_digest, simulate

SMALL = Path(__file__).resolve().parents[1] / "configs" / "small.yaml"


def scenarios(run: Path) -> int:
    files = list((run / "labels" / "benign_scenarios").glob("*.parquet"))
    return sum(pl.read_parquet(f).height for f in files)


def test_realism_parsing_and_scaling() -> None:
    assert parse_realism({}) == RealismConfig()
    parsed = parse_realism({"hard_negative_scale": 2, "no_show_rate": 0.1, "cancel_rate": None})
    assert parsed.hard_negative_scale == 2.0 and parsed.no_show_rate == 0.1
    assert parsed.cancel_rate is None
    with pytest.raises(ValueError, match="hard_negative_scale"):
        parse_realism({"hard_negative_scale": -1})
    profile = load_config(SMALL).clinics[1].profile
    doubled = RealismConfig(hard_negative_scale=2.0, no_show_rate=0.2).apply(profile)
    assert doubled.cover_days_per_month == 2 * profile.cover_days_per_month
    assert doubled.hard_negatives.pharmacy_checks_per_day == pytest.approx(
        2 * profile.hard_negatives.pharmacy_checks_per_day
    )
    assert doubled.hard_negatives.nurse_lab_followup_rate <= 1.0
    assert doubled.no_show_rate == 0.2 and doubled.cancel_rate == profile.cancel_rate
    assert load_config(Path("sim/configs/v1.yaml")).realism == RealismConfig()


def test_config_digest_tracks_generator_settings() -> None:
    cfg = load_config(SMALL)
    assert config_digest(cfg) == config_digest(replace(cfg, name="other name"))
    assert config_digest(cfg) != config_digest(
        replace(cfg, realism=RealismConfig(hard_negative_scale=0.5))
    )
    assert config_digest(cfg) != config_digest(replace(cfg, seed=cfg.seed + 1))


def test_hard_negative_scale_changes_the_hard_negatives(tmp_path: Path) -> None:
    cfg = replace(load_config(SMALL), days=10, start_date=date(2026, 1, 26))
    counts = {}
    for scale in (0.5, 2.0):
        out = tmp_path / str(scale)
        simulate(replace(cfg, realism=RealismConfig(hard_negative_scale=scale)), out, workers=1)
        counts[scale] = scenarios(out)
    assert counts[2.0] > counts[0.5]


def test_incidental_share_and_realism_measures(tmp_path: Path) -> None:
    base = replace(load_config(SMALL), days=21, start_date=date(2026, 1, 5))
    measured = {}
    for share in (0.0, 1.0):
        attacks = AttackConfig(
            campaigns_per_clinic_month=20, first_day=2, types=(6,), incidental_share=share
        )
        out = tmp_path / f"incidental-{share}"
        simulate(replace(base, attacks=attacks), out, workers=1)
        measured[share] = measure(out)
    for report in measured.values():
        for key in ("hard_negative_share", "no_show_or_cancelled_share", "clinician_booked_share"):
            assert 0.0 < report[key] < 1.0
        assert report["snooping_accesses"] > 0
    assert measured[1.0]["incidental_snooping_share"] > measured[0.0]["incidental_snooping_share"]
    assert measured[1.0]["incidental_snooping_share"] > 0.5
