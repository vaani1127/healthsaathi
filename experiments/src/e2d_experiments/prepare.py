"""Prepare a SaathiBench run for experiments: simulate, split, explain and featurise.

Nothing here reads labels; the separability audit, which does, is run by the runner.
Explanations use the shared e2d-core engine through the in-memory repository; forgery flags are
evaluated 24 hours after each access, as the nightly rescoring job would see them. Results are
cached under `<run>/derived/`.
"""

import json
import multiprocessing
import uuid
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import polars as pl

from e2d_core.explain import AccessEvent, EvidenceQuery, TemplateConfig, default_config, explain
from e2d_core.features import build_features, explanation_rows
from e2d_core.repo.memory import COLUMNS, MemoryRepository
from saathibench.config import load_config
from saathibench.coverage import clinic_keys, read
from saathibench.run import simulate
from saathibench.splits import make_splits

RESCORE_AFTER = timedelta(hours=24)
FEATURE_WORKERS = 2
FEATURE_TABLES = (
    "shifts", "sessions", "devices", "patients", "users", "memberships", "appointments",
    "encounters", "referrals", "care_team_assignments", "lab_orders", "lab_results", "vitals",
)  # fmt: skip
META = ("access_event_id", "clinic_id", "user_id", "patient_id", "role", "at", "day")


def run_dir(root: Path, name: str, seed: int) -> Path:
    return root / "runs" / f"{name}-s{seed}"


def simulate_run(sim_config: Path, root: Path, name: str, seed: int, **overrides: Any) -> Path:
    """Simulate once per seed (reused if it already exists) and write its splits."""
    out = run_dir(root, name, seed)
    if not (out / "manifest.json").exists():
        cfg = replace(load_config(sim_config), seed=seed, name=f"{name}-s{seed}", **overrides)
        simulate(cfg, out)
    if not (out / "splits.json").exists():
        make_splits(out, seed)
    return out


def template_config(templates: Iterable[str] | None) -> TemplateConfig:
    config = default_config()
    if templates is None:
        return config
    keep = set(templates)
    return replace(config, templates={k: v for k, v in config.templates.items() if k in keep})


def _explain_clinic(args: tuple[Path, str, str, tuple[str, ...] | None]) -> Path:
    run, key, timezone, templates = args
    tag = "all" if templates is None else "-".join(sorted(templates))
    out = run / "derived" / f"explanations-{tag}" / f"{key}.parquet"
    if out.exists():
        return out
    config = template_config(templates)
    events = read(run, "tables", "access_events", key)
    assert events is not None
    frames = {
        t: f.select(c) for t, c in COLUMNS.items() if (f := read(run, "tables", t, key)) is not None
    }
    repo = MemoryRepository(frames)
    results = []
    for row in events.iter_rows(named=True):
        event = AccessEvent(
            id=uuid.UUID(row["id"]),
            clinic_id=uuid.UUID(row["clinic_id"]),
            user_id=uuid.UUID(row["user_id"]),
            role=row["role"],
            patient_id=uuid.UUID(row["patient_id"]),
            resource=row["resource_type"],
            action=row["action"],
            at=row["at"],
            break_glass_id=uuid.UUID(row["break_glass_id"]) if row["break_glass_id"] else None,
        )
        as_of = event.at + RESCORE_AFTER
        query = EvidenceQuery(
            event.clinic_id, event.user_id, event.patient_id, event.at, timezone, event.role, as_of
        )
        result = explain(event, repo.evidence_for_sync(query), config, timezone, as_of)
        results.append((row["id"], result, event.break_glass_id is not None))
    out.parent.mkdir(parents=True, exist_ok=True)
    explanation_rows(results).write_parquet(out)
    return out


def _features_clinic(args: tuple[Path, str, str, tuple[str, ...] | None]) -> Path:
    run, key, timezone, templates = args
    tag = "all" if templates is None else "-".join(sorted(templates))
    out = run / "derived" / f"features-{tag}" / f"{key}.parquet"
    if out.exists():
        return out
    events = read(run, "tables", "access_events", key)
    assert events is not None
    frames = {t: f for t in FEATURE_TABLES if (f := read(run, "tables", t, key)) is not None}
    explanations = pl.read_parquet(run / "derived" / f"explanations-{tag}" / f"{key}.parquet")
    features = build_features(events, frames, explanations, role_onehot=True)
    local = pl.col("at").dt.convert_time_zone(timezone)
    meta = events.select(
        pl.col("id").alias("access_event_id"),
        "clinic_id",
        "user_id",
        "patient_id",
        "role",
        "at",
        local.dt.date().alias("day"),
        (local.dt.hour() + local.dt.minute() / 60).alias("hour_local"),
        local.dt.weekday().alias("weekday"),
        (pl.col("decision") == "deny").cast(pl.Int8).alias("refused"),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    meta.join(features, on="access_event_id", how="left", maintain_order="left").write_parquet(out)
    return out


def prepare(
    run: Path, templates: Iterable[str] | None = None, workers: int | None = None
) -> pl.DataFrame:
    """Explanations and features for every access of the run (cached). Returns the features."""
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    chosen = tuple(sorted(templates)) if templates is not None else None
    jobs = [(run, key, manifest["timezone"], chosen) for key in clinic_keys(run)]
    workers = max(1, min(workers or (multiprocessing.cpu_count() - 1), len(jobs)))
    if workers == 1:
        for job in jobs:
            _explain_clinic(job)
            _features_clinic(job)
    else:
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            list(pool.map(_explain_clinic, jobs))
        # Feature building is quick but needs about 2 GB for the largest v1 clinics, so only a
        # couple run at once.
        with ProcessPoolExecutor(
            max_workers=min(workers, FEATURE_WORKERS), mp_context=context
        ) as pool:
            list(pool.map(_features_clinic, jobs))
    tag = "all" if chosen is None else "-".join(chosen)
    return pl.concat(
        [
            pl.read_parquet(p)
            for p in sorted((run / "derived" / f"features-{tag}").glob("*.parquet"))
        ]
    )
