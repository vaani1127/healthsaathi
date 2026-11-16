"""The product's daily final ranking gives the same top-B set as the experiments' E2D path.

A small SaathiBench run is loaded into the product database with the demo loader (every patient
kept, so nothing is cut), and the same clinic days are ranked twice with one fitted model:

- experiment path: `e2d_experiments.prepare` (explanations as of 24 hours after each access over
  the in-memory backend, features), `E2D.score` with the model, `e2d_core.detect.budget`, on the
  staff accesses that `evaluate.evaluable` keeps (there is no attack type 8 in this run);
- product path: `app.detect.daily.finalise_day` with the same model.

It also checks that every staff access of those days gets the same two late forgery flags on
both paths (the experiments' explanations as of 24 hours later, kept with null for undecided).

The loader shifts every time by whole weeks, so a simulated day maps to the loaded day by that
shift; access event ids are kept.
"""

import uuid
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.core.db import TenantContext, tenant_session
from app.detect import daily, service
from app.detect.days import ready_after
from e2d_core.detect import ModelBundle, budget, fit, gate
from e2d_core.explain import AccessEvent, EvidenceQuery, explain
from e2d_core.features import ALL_FEATURES
from e2d_core.repo.memory import COLUMNS, MemoryRepository
from e2d_experiments.methods import E2D
from e2d_experiments.prepare import prepare
from hs_demo import load as loader
from saathibench.config import load_config
from saathibench.coverage import clinic_keys, read
from saathibench.run import simulate

TODAY = date(2026, 10, 13)
B = 5
IST = ZoneInfo("Asia/Kolkata")
PROFILES = Path(__file__).resolve().parents[3] / "sim" / "profiles"
RUN = """
name: daily-parity
seed: 5
start_date: "2026-01-05"
days: 7
timezone: Asia/Kolkata
clinics:
  - {profile: solo_gp, count: 2}
attacks:
  campaigns_per_clinic_month: 30.0
  first_day: 1
  types: [1, 2, 10]
"""
# Simulated days ranked by both paths (each needs the 24 hours after it inside the run).
DAYS = (2, 3, 4)


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("parity-sim")
    config = folder / "run.yaml"
    config.write_text(RUN, encoding="utf-8")
    simulate(load_config(config, PROFILES), folder / "run", workers=1)
    return folder / "run"


@pytest.fixture(scope="module")
def experiment(run: Path) -> pl.DataFrame:
    return prepare(run, workers=1)


@pytest.fixture(scope="module")
async def loaded(demo_app: str, run: Path) -> timedelta:
    """Load every patient; returns the time shift the loader applied."""
    tables, _, _ = loader.read_run(run)
    report = await loader.load(run, "demo-password-123", TODAY, per_clinic=100_000)
    assert report.access_events == tables["access_events"].height, "every access was loaded"
    last = tables["access_events"]["at"].max()
    return loader.week_shift(last.date(), TODAY)  # type: ignore[union-attr]


LATE = ("no_progress", "cancelled_after_access")


def experiment_late(run: Path) -> dict[str, dict[str, bool | None]]:
    """The experiments' late flags per access (null where the flag is undecided)."""
    frames = sorted((run / "derived" / "explanations-all").glob("*.parquet"))
    rows = pl.concat([pl.read_parquet(f) for f in frames])
    return {
        r["access_event_id"]: {n: r[f"flag_{n}"] for n in LATE}
        for r in rows.select("access_event_id", *(f"flag_{n}" for n in LATE)).iter_rows(named=True)
    }


def experiment_top(frame: pl.DataFrame, clinic: str, day: date, model: ModelBundle) -> list[str]:
    """E2D's alerts for one clinic day, as the registered evaluation makes them."""
    test = frame.filter(
        (pl.col("clinic_id") == clinic)
        & (pl.col("day") == day)
        & (pl.col("role") != "patient")
        & (pl.col("break_glass") == 0)
    )
    method = E2D()
    method.models, method.fallback = {clinic: model}, model
    scores = method.score(test)
    scored = test.select("access_event_id", "clinic_id", "day", "break_glass").with_columns(
        pl.Series("score", scores), pl.lit(True).alias("gated")
    )
    ranked = budget(scored, B).filter(pl.col("alert")).sort("rank_in_day")
    return ranked["access_event_id"].to_list()


async def test_product_and_experiment_pick_the_same_top_b(
    run: Path, experiment: pl.DataFrame, loaded: timedelta, tmp_path: Path
) -> None:
    start = date(2026, 1, 5)
    compared = 0
    expected_late = experiment_late(run)
    late_compared = 0
    statuses: Counter[str] = Counter()
    for clinic in sorted(experiment["clinic_id"].unique().to_list()):
        rows = experiment.filter((pl.col("clinic_id") == clinic) & (pl.col("role") != "patient"))
        # One model per clinic, fitted on its gated accesses of the first two days, used by both.
        train = rows.filter(pl.col("day") < start + timedelta(days=DAYS[0]))
        model = fit(train.filter(gate(train)), ALL_FEATURES, "iforest", 0, clinic)
        store = service.ModelStore(tmp_path / clinic)
        for offset in DAYS:
            sim_day = start + timedelta(days=offset)
            expected = experiment_top(experiment, clinic, sim_day, model)
            day = sim_day + loaded
            ctx = TenantContext(clinic_id=uuid.UUID(clinic), role="system")
            async with tenant_session(ctx) as db:
                product = await daily.day_features(db, uuid.UUID(clinic), day, "Asia/Kolkata")
            for event_id, late in product.late.items():
                want = expected_late[event_id]
                assert late.status in ("decided", "not_applicable"), event_id
                statuses[late.status] += 1
                assert late.flags == want, (event_id, late, want)
                late_compared += 1
            result = await daily.finalise_day(
                uuid.UUID(clinic),
                day,
                now=ready_after(day, IST) + timedelta(minutes=1),
                run=daily.new_run(store, b=B),
                model=model,
            )
            assert result is not None
            got = result.payload["final"]
            assert set(got) == set(expected), (clinic, sim_day, got, expected)
            assert got == expected, "same order too"
            assert result.payload["late_flags_undecided"] == 0
            compared += len(expected)
    assert compared > 0, "the run has alerts to compare"
    assert late_compared > 0
    flagged = sum(1 for v in expected_late.values() if any(v[n] for n in LATE))
    assert flagged > 0, "some accesses in the run have a late flag set, so the comparison bites"
    print(
        "\nparity:",
        f"{compared} final alerts and the late flags of {late_compared} accesses compared;",
        f"{flagged} accesses in the run have a late flag set;",
        f"late flag status {dict(statuses)}",
    )


def test_the_template_never_changes_between_the_access_and_24_hours_later(run: Path) -> None:
    """On simulated data the product's stored explanation (made at the access) and the
    experiments' re-explanation (as of 24 hours later) pick the same template and evidence, so
    computing late flags for the stored evidence matches the experiments."""
    total = differ = 0
    for key in clinic_keys(run):
        events = read(run, "tables", "access_events", key)
        assert events is not None
        frames = {}
        for table, columns in COLUMNS.items():
            frame = read(run, "tables", table, key)
            if frame is not None:
                frames[table] = frame.select(columns)
        repo = MemoryRepository(frames)
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
            base = (event.clinic_id, event.user_id, event.patient_id, event.at, "Asia/Kolkata")
            at_access = explain(
                event, repo.evidence_for_sync(EvidenceQuery(*base, event.role)), timezone=base[4]
            )
            later = event.at + timedelta(hours=24)
            at_later = explain(
                event,
                repo.evidence_for_sync(EvidenceQuery(*base, event.role, later)),
                timezone=base[4],
                as_of=later,
            )
            total += 1
            differ += (at_access.template_code, at_access.evidence) != (
                at_later.template_code,
                at_later.evidence,
            )
    print(f"\n{differ} of {total} accesses change template or evidence 24 hours later")
    assert total > 0 and differ == 0
