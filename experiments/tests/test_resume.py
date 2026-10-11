"""A finished seed is saved and reused when the run starts again with the same settings, and run
again when a setting that shapes results changed."""

from pathlib import Path
from typing import Any

import pytest
from omegaconf import OmegaConf

from e2d_experiments import run

ROWS = [
    {"seed": 0, "split": "temporal", "ablation": "none", "method": "e2d", "budget": 5,
     "metric": "recall", "group": None, "value": 0.25},
    {"seed": 0, "split": "temporal", "ablation": "none", "method": "-", "budget": 0,
     "metric": "coverage", "group": None, "value": float("nan")},
]  # fmt: skip
CLINICS = [
    {"seed": 0, "split": "temporal", "ablation": "none", "method": "e2d", "budget": 5,
     "clinic_index": 3, "attacks": 4, "hits": 1, "alerts": 9},
]  # fmt: skip


def test_finished_seeds_are_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_run_seed(cfg: Any, seed: int, out: Path) -> tuple[list[Any], list[Any]]:
        calls.append(seed)
        return ROWS, CLINICS

    monkeypatch.setattr(run, "run_seed", fake_run_seed)
    cfg = OmegaConf.create({"seeds": [0], "out": str(tmp_path), "budgets": [5], "workers": 3})
    first = run.seed_results(cfg, 0, tmp_path)
    # Another worker count or output folder does not change results: the seed is reused.
    same = OmegaConf.create({**OmegaConf.to_container(cfg), "workers": 1})  # type: ignore[dict-item]
    again = run.seed_results(same, 0, tmp_path)
    assert calls == [0]
    assert str(again) == str(first)  # equal, NaN included
    # A setting that shapes results makes the seed run again.
    other = OmegaConf.create({**OmegaConf.to_container(cfg), "budgets": [3]})  # type: ignore[dict-item]
    run.seed_results(other, 0, tmp_path)
    assert calls == [0, 0]
