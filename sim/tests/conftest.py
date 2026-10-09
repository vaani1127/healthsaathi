from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from saathibench.config import AttackConfig, load_config
from saathibench.run import simulate

SMALL = Path(__file__).resolve().parents[1] / "configs" / "small.yaml"


@pytest.fixture(scope="session")
def forgery_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Six weeks of the small config with many attacks, most of them forgery."""
    cfg = replace(
        load_config(SMALL),
        days=42,
        start_date=date(2026, 1, 5),
        attacks=AttackConfig(campaigns_per_clinic_month=16, first_day=3, types=(10, 10, 1, 6)),
    )
    out = tmp_path_factory.mktemp("forgery") / "run"
    simulate(cfg, out, workers=1)
    return out
