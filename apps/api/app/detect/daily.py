"""Daily final ranking of each clinic's accesses (SPEC 5.5, ADR 0008).

The per-minute scorer (`service.score_clinic`) raises provisional alerts with the forgery flags
known when the access happened. Two flags need what happened in the 24 hours after the access
(`no_progress`, `cancelled_after_access`), so the final list of a clinic day D is made once every
access of D is at least 24 hours old: from 00:00 clinic time on D+2.

For day D the job:

1. Computes the two late flags of every staff access of D as of 24 hours after that access,
   with e2d-core's `forgery_flags`, for the evidence its stored explanation names (the
   appointment, lab order, referral, care team row or invoice it was explained by when it
   happened). The template is not chosen again. If that record cannot be found, the flags stay
   undecided and the access id is recorded.
2. Builds the features of D's accesses with those flags (the same e2d-core feature code).
3. Fits a final model on the gated staff accesses of the 30 days before D: one for the clinic if
   it has at least MIN_CLINIC_ROWS of them, otherwise one over every clinic. With nothing to fit
   on, the starter rules rank the day.
4. Ranks D's gated staff accesses, break-glass excluded, and keeps the top B with
   `e2d_core.detect.budget`, as the experiments do.
5. In one transaction: adds alerts for final accesses that have none, marks unreviewed
   provisional alerts final or retired, leaves reviewed alerts untouched, and writes one audit
   event (kind `detect_daily`). That audit event is what marks the day finalised.

Safe to rerun: a session advisory lock lets one run go ahead at a time, and a day that already
has its audit event is never computed again. A crash before the commit leaves the day not
finalised, so the next run redoes it. Days older than the catch-up window
(DETECT_CATCH_UP_DAYS, 7 by default) get a `skipped` audit event. Days before the clinic's first
staff access are not due: they are neither finalised nor skipped, however wide the window is, and
a clinic with no staff access has nothing due.

The worker caps each run (`max_days`, `max_cache_days`) so that one tick finalises at most one
clinic day with accesses and computes at most a few days of training features; the rest waits
for the next tick. Detection's per-minute scoring and the anchor timer share the worker's event
loop, so an uncapped run would hold them up. The command line runs without caps.

The features of finalised days are kept in a per-clinic cache file in MODEL_DIR, only so the
30-day training window does not need recomputing every night. It holds access event ids, days,
flags and feature values. It is never the source of truth: if it is missing or unreadable it is
rebuilt from the database.

    uv run python -m app.detect.job --daily
"""

import hashlib
import json
import logging
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import db_now
from app.core.config import get_settings
from app.core.db import TenantContext, get_engine, tenant_session
from app.db.enums import AlertStatus
from app.db.models import AccessExplanation, Alert, AlertReview, Clinic
from app.detect import starter_rules
from app.detect.days import (
    CATCH_UP_DAYS,
    DAILY_KIND,
    Outcome,
    day_bounds,
    due_window,
    entries,
    entry_for,
    ready_after,
)
from app.detect.loader import load_clinic
from app.detect.service import (
    ModelStore,
    Stage,
    clinic_ids,
    model_store,
    stage_of,
)
from app.ledger.writer import advisory_key, append_audit_event, lock_clinic
from e2d_core.detect import MIN_CLINIC_ROWS, ModelBundle, budget, dumps, fit, gate, model_version
from e2d_core.explain import AccessEvent as CoreAccessEvent
from e2d_core.explain import EvidenceQuery, default_config, forgery
from e2d_core.explain.model import Candidate, EvidenceRef
from e2d_core.features import ALL_FEATURES, build_features
from e2d_core.repo.sql import SqlRepository

logger = logging.getLogger(__name__)

SYSTEM = "system"
LATE_AFTER = timedelta(hours=24)
TRAIN_DAYS = 30
LATE_FLAGS = ("no_progress", "cancelled_after_access")
AS_OF_RULE = "forgery flags of each access as of 24 hours after that access"
SEED = 0
LOCK_KEY = advisory_key("detect-daily", uuid.UUID(int=0))


class LateStatus:
    DECIDED = "decided"  # computed for the record the stored explanation names
    NOT_APPLICABLE = "not_applicable"  # its evidence is not one the late flags look at
    UNDECIDED = "undecided"  # the record the stored explanation names was not found
    DENIED = "denied"  # a denied access stored without an explanation
    DAY = "day"  # cache marker row: this day has been computed


# Late flags -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LateResult:
    status: str
    flags: dict[str, bool | None]


UNDECIDED: dict[str, bool | None] = dict.fromkeys(LATE_FLAGS)
# Evidence whose record the late flags look at (the pools of e2d_core.explain.forgery._find).
LATE_KINDS = frozenset({"appointment", "lab_order", "referral", "care_team", "invoice"})


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def flag_ref(template: str, refs: tuple[EvidenceRef, ...]) -> EvidenceRef:
    """The evidence the forgery flags describe (the same choice as forgery_flags: the future
    appointment for T_FOLLOWUP, otherwise the first ref)."""
    return refs[-1] if template == "T_FOLLOWUP" else refs[0]


async def late_flags(
    db: AsyncSession, clinic_id: uuid.UUID, events: pl.DataFrame, timezone: str
) -> dict[str, LateResult]:
    """The late flags of each access in `events`: computed with e2d-core's forgery_flags for the
    evidence its stored explanation names, as of 24 hours after the access. The template is not
    chosen again. Accesses whose evidence the late flags do not look at (or with no explanation)
    are not_applicable, with both flags None, as the engine gives them."""
    if events.is_empty():
        return {}
    x = AccessExplanation
    stored = {
        str(r.access_event_id): r
        for r in (
            await db.execute(
                select(x.access_event_id, x.template_code, x.strength, x.evidence).where(
                    x.clinic_id == clinic_id,
                    x.access_event_id.in_([uuid.UUID(i) for i in events["id"]]),
                )
            )
        ).all()
    }
    repo = SqlRepository(db)
    config = default_config().forgery
    tz = ZoneInfo(timezone)
    out: dict[str, LateResult] = {}
    for row in events.iter_rows(named=True):
        found = stored.get(row["id"])
        template = found.template_code if found else None
        evidence: dict[str, Any] = (found.evidence if found else None) or {}
        refs = tuple(EvidenceRef(r["kind"], uuid.UUID(r["id"])) for r in evidence.get("refs", []))
        if template is None or not refs:
            status = LateStatus.DENIED if row["decision"] == "deny" else LateStatus.NOT_APPLICABLE
            out[row["id"]] = LateResult(status, dict(UNDECIDED))
            continue
        ref = flag_ref(template, refs)
        if ref.kind not in LATE_KINDS:
            # Queue tokens, registrations, reasons, break-glass: the late flags do not apply.
            out[row["id"]] = LateResult(LateStatus.NOT_APPLICABLE, dict(UNDECIDED))
            continue
        user_id = uuid.UUID(row["user_id"])
        patient_id = uuid.UUID(row["patient_id"])
        event = CoreAccessEvent(
            id=uuid.UUID(row["id"]),
            clinic_id=clinic_id,
            user_id=user_id,
            role=row["role"],
            patient_id=patient_id,
            resource=row["resource_type"],
            action=row["action"],
            at=row["at"],
            break_glass_id=_uuid(row["break_glass_id"]),
        )
        as_of = event.at + LATE_AFTER
        bundle = await repo.evidence_for(
            EvidenceQuery(clinic_id, user_id, patient_id, event.at, timezone, row["role"], as_of)
        )
        item = forgery._find(bundle, ref.kind, ref.id)
        if item is None:
            out[row["id"]] = LateResult(LateStatus.UNDECIDED, dict(UNDECIDED))
            continue
        strength = float(found.strength) if found is not None else 0.0
        best = Candidate(template, strength, refs, created_at=item.created_at)
        flags = forgery.forgery_flags(event, best, bundle, config, tz, as_of)
        out[row["id"]] = LateResult(LateStatus.DECIDED, {n: flags.get(n) for n in LATE_FLAGS})
    return out


# Features of one day ----------------------------------------------------------------------------

FEATURE_SCHEMA: dict[str, Any] = {
    "day": pl.Date,
    "access_event_id": pl.String,
    "late_status": pl.String,
    **dict.fromkeys(ALL_FEATURES, pl.Float64),
}
STAFF = pl.col("role") != "patient"


def empty_features() -> pl.DataFrame:
    return pl.DataFrame(schema=FEATURE_SCHEMA)


@dataclass
class DayFeatures:
    """Feature rows of one day's staff accesses with their late flags, plus what the final
    alerts need that the cache does not hold."""

    rows: pl.DataFrame
    policy_version: dict[str, str] = field(default_factory=dict)
    late: dict[str, LateResult] = field(default_factory=dict)


async def day_features(
    db: AsyncSession, clinic_id: uuid.UUID, day: date, timezone: str
) -> DayFeatures:
    start, end = day_bounds(day, ZoneInfo(timezone))
    events, frames, explanations = await load_clinic(db, clinic_id, start, end)
    today = events.filter((pl.col("at") >= start) & STAFF)
    if today.is_empty():
        return DayFeatures(empty_features())
    late = await late_flags(db, clinic_id, today, timezone)
    ids = list(late)
    patch = pl.DataFrame(
        {
            "access_event_id": ids,
            "_status": [late[i].status for i in ids],
            **{f"_{n}": [late[i].flags[n] for i in ids] for n in LATE_FLAGS},
        },
        schema={
            "access_event_id": pl.String,
            "_status": pl.String,
            **{f"_{n}": pl.Boolean for n in LATE_FLAGS},
        },
    )
    explanations = (
        explanations.join(patch, on="access_event_id", how="left", maintain_order="left")
        .with_columns(
            pl.when(pl.col("_status").is_not_null())
            .then(pl.col(f"_{n}"))
            .otherwise(pl.col(f"flag_{n}"))
            .alias(f"flag_{n}")
            for n in LATE_FLAGS
        )
        .drop("_status", *(f"_{n}" for n in LATE_FLAGS))
    )
    features = build_features(events, frames, explanations).join(
        patch.select("access_event_id", pl.col("_status").alias("late_status")),
        on="access_event_id",
        how="inner",
        maintain_order="left",
    )
    rows = features.select(
        pl.lit(day, dtype=pl.Date).alias("day"),
        "access_event_id",
        "late_status",
        *(pl.col(c).cast(pl.Float64).fill_null(0.0) for c in ALL_FEATURES),
    )
    return DayFeatures(
        rows,
        dict(zip(today["id"].to_list(), today["policy_version"].to_list(), strict=True)),
        late,
    )


# Feature cache ----------------------------------------------------------------------------------

CACHE_KEY = hashlib.sha256(
    json.dumps(
        {"version": 1, "features": ALL_FEATURES, "late_after_hours": 24, "flags": LATE_FLAGS}
    ).encode()
).hexdigest()[:12]


class FeatureCache:
    """Feature rows of computed days for one clinic, in MODEL_DIR/daily. Rebuildable: a missing
    or unreadable file means the days are computed again from the database."""

    def __init__(self, root: Path, clinic_id: uuid.UUID) -> None:
        self.path = root / "daily" / f"{clinic_id}.{CACHE_KEY}.parquet"
        self.clinic_id = clinic_id
        self.frame = self._read()
        self.dirty = False

    def _read(self) -> pl.DataFrame:
        if not self.path.exists():
            logger.info(
                "feature cache missing; it is rebuilt from the database as days are needed",
                extra={"clinic_id": str(self.clinic_id)},
            )
            return empty_features()
        try:
            frame = pl.read_parquet(self.path)
            if dict(frame.schema) != FEATURE_SCHEMA:
                raise ValueError("unexpected columns")
            return frame
        except Exception as exc:
            logger.warning(
                "feature cache unreadable; rebuilding it from the database",
                extra={"clinic_id": str(self.clinic_id), "error": str(exc)},
            )
            self.path.unlink(missing_ok=True)
            return empty_features()

    def days(self) -> set[date]:
        return set(self.frame.filter(pl.col("late_status") == LateStatus.DAY)["day"].to_list())

    def put(self, day: date, rows: pl.DataFrame) -> None:
        marker = pl.DataFrame(
            {"day": [day], "access_event_id": [None], "late_status": [LateStatus.DAY]},
            schema={k: FEATURE_SCHEMA[k] for k in ("day", "access_event_id", "late_status")},
        ).with_columns(pl.lit(0.0).alias(c) for c in ALL_FEATURES)
        self.frame = pl.concat(
            [self.frame.filter(pl.col("day") != day), rows.select(list(FEATURE_SCHEMA)), marker]
        ).sort("day", "access_event_id", nulls_last=True)
        self.dirty = True

    def rows(self, days: Sequence[date]) -> pl.DataFrame:
        return self.frame.filter(
            pl.col("day").is_in(list(days)) & (pl.col("late_status") != LateStatus.DAY)
        )

    def prune(self, oldest: date) -> None:
        before = self.frame.height
        self.frame = self.frame.filter(pl.col("day") >= oldest)
        self.dirty = self.dirty or self.frame.height != before

    def save(self) -> str:
        """Write the file if it changed; return its sha256."""
        if self.dirty or not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            self.frame.write_parquet(tmp)
            tmp.replace(self.path)
            self.dirty = False
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


# One run ----------------------------------------------------------------------------------------


@dataclass
class Run:
    """State shared by the clinics of one run: their time zones and caches, and how much work
    the run may still do (None: no limit)."""

    store: ModelStore
    budget: int
    scorer: str
    catch_up: int = CATCH_UP_DAYS
    # Clinic days with staff accesses this run may still finalise.
    days_left: int | None = None
    # Days of training features this run may still compute.
    cache_days_left: int | None = None
    timezones: dict[uuid.UUID, str] = field(default_factory=dict)
    caches: dict[uuid.UUID, FeatureCache] = field(default_factory=dict)
    # Global models already fitted in this run, by the first day of their window.
    pooled: dict[date, tuple[ModelBundle | None, int]] = field(default_factory=dict)

    def cache(self, clinic_id: uuid.UUID) -> FeatureCache:
        if clinic_id not in self.caches:
            self.caches[clinic_id] = FeatureCache(self.store.root, clinic_id)
        return self.caches[clinic_id]

    async def timezone(self, clinic_id: uuid.UUID) -> str:
        if clinic_id not in self.timezones:
            async with tenant_session(_ctx(clinic_id)) as db:
                tz = await db.scalar(select(Clinic.timezone).where(Clinic.id == clinic_id))
            self.timezones[clinic_id] = tz or "Asia/Kolkata"
        return self.timezones[clinic_id]

    async def training_rows(self, clinic_id: uuid.UUID, days: Sequence[date]) -> pl.DataFrame:
        """Gated feature rows of `days`, computing the days the cache does not hold."""
        cache = self.cache(clinic_id)
        missing = [d for d in days if d not in cache.days()]
        allowed = missing if self.cache_days_left is None else missing[: self.cache_days_left]
        if allowed:
            timezone = await self.timezone(clinic_id)
            for d in allowed:
                async with tenant_session(_ctx(clinic_id)) as db:
                    cache.put(d, (await day_features(db, clinic_id, d, timezone)).rows)
            cache.save()
            if self.cache_days_left is not None:
                self.cache_days_left -= len(allowed)
        if len(allowed) < len(missing):
            raise DeferredError(f"{len(missing) - len(allowed)} training days left to compute")
        rows = cache.rows(days)
        return rows.filter(gate(rows)) if rows.height else rows

    async def final_model(
        self, clinic_id: uuid.UUID, day: date
    ) -> tuple[ModelBundle | None, str, int, list[date]]:
        """(model, scope, training rows, window) for day `day`: fitted only on the TRAIN_DAYS
        days before it."""
        window = [day - timedelta(days=k) for k in range(TRAIN_DAYS, 0, -1)]
        own = await self.training_rows(clinic_id, window)
        if own.height >= MIN_CLINIC_ROWS:
            return (
                fit(own, ALL_FEATURES, self.scorer, SEED, str(clinic_id)),
                "clinic",
                own.height,
                window,
            )
        if window[0] not in self.pooled:
            parts = [own] + [
                await self.training_rows(other, window)
                for other in await clinic_ids()
                if other != clinic_id
            ]
            pooled = (
                pl.concat([p for p in parts if p.height]) if any(p.height for p in parts) else None
            )
            self.pooled[window[0]] = (
                (fit(pooled, ALL_FEATURES, self.scorer, SEED), pooled.height)
                if pooled is not None
                else (None, 0)
            )
        bundle, n = self.pooled[window[0]]
        return bundle, ("global" if bundle is not None else starter_rules.NAME), n, window


class DeferredError(Exception):
    """The run reached its limit before the day could be finalised; a later run continues."""


def _ctx(clinic_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(clinic_id=clinic_id, role=SYSTEM)


def rank_day(rows: pl.DataFrame, bundle: ModelBundle | None, b: int) -> pl.DataFrame:
    """The day's rows with `raw` (the scorer's output), `rank_in_day` and `alert`. Ranking is the
    experiments' E2D ranking: gated rows only, break-glass left out, highest score first, ties by
    access event id (`e2d_core.detect.budget`)."""
    if rows.is_empty():
        return rows.with_columns(
            pl.lit(None, pl.Float64).alias("raw"),
            pl.lit(None, pl.Int64).alias("rank_in_day"),
            pl.lit(False).alias("alert"),
        )
    raw = bundle.score(rows) if bundle is not None else starter_rules.score(rows)
    gated = gate(rows).to_numpy()
    eligible = gated & (rows["break_glass"].to_numpy() == 0)
    # As E2D.score in the experiments: gated scores shifted above 0, the rest 0.
    shifted = np.where(gated, raw - raw[eligible].min() + 1e-6 if eligible.any() else raw, 0.0)
    scored = rows.with_columns(
        pl.lit("clinic").alias("_clinic"),
        pl.Series("score", shifted),
        pl.Series("gated", gated),
        pl.Series("raw", raw),
    )
    return budget(scored, b, key=("_clinic", "day")).drop("_clinic", "score", "gated")


@dataclass
class DayResult:
    clinic_id: str
    day: date
    outcome: str
    payload: dict[str, Any]


async def finalise_day(
    clinic_id: uuid.UUID,
    day: date,
    *,
    now: datetime | None = None,
    run: Run | None = None,
    model: ModelBundle | None = None,
    before_commit: Callable[[], None] | None = None,
) -> DayResult | None:
    """Make the final alert list of one clinic day. Returns None when the day is already
    finalised (by this or another run). `model` replaces the fitted final model (tests);
    `before_commit` runs just before the transaction ends (tests)."""
    run = run or new_run()
    timezone = await run.timezone(clinic_id)
    tz = ZoneInfo(timezone)
    async with tenant_session(_ctx(clinic_id)) as db:
        now = now or await db_now(db)
        if now < ready_after(day, tz):
            raise ValueError(f"{day} is not ready until {ready_after(day, tz).isoformat()}")
        if await entry_for(db, clinic_id, day) is not None:
            return None
        today = await day_features(db, clinic_id, day, timezone)

    bundle: ModelBundle | None
    window: list[date]
    if model is not None:
        bundle, scope, trained, window = model, "given", model.trained_rows, []
    elif today.rows.is_empty():
        # Nothing to rank, so no model is fitted.
        bundle, scope, trained, window = None, "none", 0, []
    else:
        bundle, scope, trained, window = await run.final_model(clinic_id, day)
    version = model_version(dumps(bundle)) if bundle is not None else starter_rules.VERSION
    scorer = bundle.scorer if bundle is not None else starter_rules.NAME
    ranked = rank_day(today.rows, bundle, run.budget)

    cache = run.cache(clinic_id)
    cache.put(day, today.rows)
    cache.prune(day - timedelta(days=TRAIN_DAYS + CATCH_UP_DAYS + 2))
    cache_sha = cache.save()

    final = ranked.filter(pl.col("alert")).sort("rank_in_day")
    gated_ids = (
        set(ranked.filter(gate(ranked))["access_event_id"].to_list()) if ranked.height else set()
    )
    undecided = sorted(
        ranked.filter(pl.col("late_status") == LateStatus.UNDECIDED)["access_event_id"].to_list()
    )

    async with tenant_session(_ctx(clinic_id)) as db:
        await lock_clinic(db, "detect-daily", clinic_id)
        if await entry_for(db, clinic_id, day) is not None:
            return None
        at = await db_now(db)
        final_ids = final["access_event_id"].to_list()
        alerts = list(
            (
                await db.scalars(
                    select(Alert)
                    .where(
                        Alert.clinic_id == clinic_id,
                        or_(
                            Alert.day == day,
                            Alert.access_event_id.in_([uuid.UUID(i) for i in final_ids]),
                        ),
                    )
                    .with_for_update()
                )
            ).all()
        )
        reviewed = set(
            (
                await db.scalars(
                    select(AlertReview.alert_id).where(
                        AlertReview.alert_id.in_([a.id for a in alerts])
                    )
                )
            ).all()
        )

        def is_reviewed(alert: Alert) -> bool:
            return alert.status != AlertStatus.OPEN or alert.id in reviewed

        by_event = {str(a.access_event_id): a for a in alerts}
        lifecycle = {"stage": Stage.FINAL, "finalised_at": at.isoformat(), "day": day.isoformat()}
        added: list[str] = []
        finalised: list[str] = []
        kept: list[str] = []
        for row in final.iter_rows(named=True):
            event_id = row["access_event_id"]
            values = {name: float(row[name]) for name in ALL_FEATURES}
            late = today.late[event_id]
            features = {
                "values": values,
                "top": bundle.top_features(values)
                if bundle is not None
                else starter_rules.fired(values),
                "scorer": scorer,
                "lifecycle": lifecycle,
                "late_flags": {"status": late.status, **late.flags},
            }
            alert = by_event.get(event_id)
            if alert is None:
                db.add(
                    Alert(
                        clinic_id=clinic_id,
                        access_event_id=uuid.UUID(event_id),
                        day=day,
                        score=float(row["raw"]),
                        rank_in_day=int(row["rank_in_day"]),
                        features=features,
                        model_version=version,
                        policy_version=today.policy_version[event_id],
                    )
                )
                added.append(event_id)
            elif is_reviewed(alert):
                kept.append(event_id)
            else:
                alert.score = float(row["raw"])
                alert.rank_in_day = int(row["rank_in_day"])
                alert.model_version = version
                alert.features = features
                finalised.append(event_id)
        retired: list[str] = []
        for alert in alerts:
            event_id = str(alert.access_event_id)
            if alert.day != day or event_id in final_ids or is_reviewed(alert):
                continue
            if stage_of(alert) == Stage.RETIRED:
                continue
            alert.features = {
                **alert.features,
                "lifecycle": {
                    "stage": Stage.RETIRED,
                    "retired_at": at.isoformat(),
                    "day": day.isoformat(),
                },
            }
            retired.append(event_id)
        outside = sorted(
            str(a.access_event_id)
            for a in alerts
            if a.day == day and is_reviewed(a) and str(a.access_event_id) not in final_ids
        )
        payload: dict[str, Any] = {
            "type": DAILY_KIND,
            "outcome": Outcome.FINALISED,
            "day": day.isoformat(),
            "timezone": timezone,
            "as_of_rule": AS_OF_RULE,
            "budget": run.budget,
            "scorer": scorer,
            "model_version": version,
            "model_scope": scope,
            "training_days": [window[0].isoformat(), window[-1].isoformat()] if window else [],
            "training_rows": trained,
            "staff_accesses": ranked.height,
            "gated": len(gated_ids),
            "final": final_ids,
            "added": added,
            "finalised": finalised,
            "retired": retired,
            "reviewed_in_final": kept,
            "reviewed_outside_final": outside,
            "added_count": len(added),
            "retired_count": len(retired),
            "late_flags_status": dict(
                sorted(Counter(ranked["late_status"].to_list()).items()) if ranked.height else []
            ),
            "late_flags_undecided": len(undecided),
            "late_flags_undecided_gated": len(gated_ids.intersection(undecided)),
            "undecided_access_event_ids": undecided,
            "cache_sha256": cache_sha,
            "at": at.isoformat(),
        }
        await db.flush()
        await append_audit_event(db, clinic_id, DAILY_KIND, payload)
        if before_commit is not None:
            before_commit()
    logger.info(
        "day finalised",
        extra={
            "clinic_id": str(clinic_id),
            "day": day.isoformat(),
            "added": len(added),
            "retired": len(retired),
            "undecided": len(undecided),
        },
    )
    return DayResult(str(clinic_id), day, Outcome.FINALISED, payload)


async def skip_day(clinic_id: uuid.UUID, day: date, reason: str) -> DayResult | None:
    async with tenant_session(_ctx(clinic_id)) as db:
        await lock_clinic(db, "detect-daily", clinic_id)
        if await entry_for(db, clinic_id, day) is not None:
            return None
        at = await db_now(db)
        payload = {
            "type": DAILY_KIND,
            "outcome": Outcome.SKIPPED,
            "day": day.isoformat(),
            "reason": reason,
            "at": at.isoformat(),
        }
        await append_audit_event(db, clinic_id, DAILY_KIND, payload)
    logger.warning("day skipped", extra={"clinic_id": str(clinic_id), "day": day.isoformat()})
    return DayResult(str(clinic_id), day, Outcome.SKIPPED, payload)


def skip_reason(catch_up: int) -> str:
    return f"older than the {catch_up} day catch-up window when the job ran"


SKIP_REASON = skip_reason(CATCH_UP_DAYS)

# Local days of a clinic's staff accesses in [after, before).
DAYS_SQL = text(
    "SELECT DISTINCT (at AT TIME ZONE :tz)::date AS day FROM access_events"
    " WHERE clinic_id = :clinic AND at >= :after AND at < :before AND role <> 'patient'"
)
FIRST_STAFF_AT = text(
    "SELECT min(at) FROM access_events WHERE clinic_id = :clinic AND role <> 'patient'"
)
# After a failed pass, a clinic is tried again after this long, not at every worker tick.
RETRY_AFTER = timedelta(minutes=15)


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FailureState:
    retry_at: ClassVar[dict[uuid.UUID, datetime]] = {}


async def first_staff_day(db: AsyncSession, clinic_id: uuid.UUID, timezone: str) -> date | None:
    """The clinic's local day of its first staff access, or None if it has none."""
    at: datetime | None = await db.scalar(FIRST_STAFF_AT, {"clinic": clinic_id})
    return at.astimezone(ZoneInfo(timezone)).date() if at is not None else None


async def run_clinic(clinic_id: uuid.UUID, now: datetime, run: Run) -> list[DayResult]:
    """Finalise the clinic's due days, oldest first, and skip older unfinished days. No day
    before the clinic's first staff access is due."""
    timezone = await run.timezone(clinic_id)
    tz = ZoneInfo(timezone)
    today = now.astimezone(tz).date()
    window = due_window(today, run.catch_up)
    async with tenant_session(_ctx(clinic_id)) as db:
        first = await first_staff_day(db, clinic_id, timezone)
        if first is None:
            return []
        done = await entries(db, clinic_id)
        before, _ = day_bounds(window[0], tz)
        # Days are handled oldest first, so every day before the newest entry has one already.
        after = day_bounds(max(done) + timedelta(days=1), tz)[0] if done else EPOCH
        older = (
            await db.scalars(
                DAYS_SQL, {"tz": timezone, "clinic": clinic_id, "after": after, "before": before}
            )
        ).all()
    results: list[DayResult] = []
    for day in sorted(d for d in older if d not in done):
        if (r := await skip_day(clinic_id, day, skip_reason(run.catch_up))) is not None:
            results.append(r)
    for day in window:
        if day < first or day in done:
            continue
        if run.days_left is not None and run.days_left <= 0:
            break
        try:
            r = await finalise_day(clinic_id, day, now=now, run=run)
        except DeferredError as exc:
            logger.info(
                "daily detection deferred",
                extra={"clinic_id": str(clinic_id), "day": day.isoformat(), "reason": str(exc)},
            )
            break
        if r is not None:
            results.append(r)
            if run.days_left is not None and r.payload.get("staff_accesses", 0) > 0:
                run.days_left -= 1
    return results


def new_run(
    store: ModelStore | None = None,
    b: int | None = None,
    *,
    catch_up: int | None = None,
    max_days: int | None = None,
    max_cache_days: int | None = None,
) -> Run:
    settings = get_settings()
    return Run(
        store=store or model_store(),
        budget=b or settings.alert_budget,
        scorer=settings.detect_scorer,
        catch_up=catch_up or settings.detect_catch_up_days,
        days_left=max_days,
        cache_days_left=max_cache_days,
    )


async def run_due(
    now: datetime | None = None,
    clinics: Sequence[uuid.UUID] | None = None,
    store: ModelStore | None = None,
    *,
    catch_up: int | None = None,
    max_days: int | None = None,
    max_cache_days: int | None = None,
) -> dict[str, list[DayResult]] | None:
    """One pass over every clinic (or `clinics`). Returns None when another run holds the lock.
    A clinic whose pass fails is logged and left for the next run. `max_days` and
    `max_cache_days` cap the work of this pass (the worker sets them); `catch_up` overrides
    DETECT_CATCH_UP_DAYS."""
    async with get_engine().connect() as lock_conn:
        got = await lock_conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})
        if not got:
            logger.info("daily detection already running")
            return None
        try:
            if now is None:
                async with tenant_session(_ctx()) as db:
                    now = await db_now(db)
            run = new_run(
                store, catch_up=catch_up, max_days=max_days, max_cache_days=max_cache_days
            )
            out: dict[str, list[DayResult]] = {}
            for clinic_id in clinics if clinics is not None else await clinic_ids():
                retry_at = FailureState.retry_at.get(clinic_id)
                if retry_at is not None and now < retry_at:
                    continue
                try:
                    out[str(clinic_id)] = await run_clinic(clinic_id, now, run)
                    FailureState.retry_at.pop(clinic_id, None)
                except Exception:
                    FailureState.retry_at[clinic_id] = now + RETRY_AFTER
                    logger.exception("daily detection failed", extra={"clinic_id": str(clinic_id)})
            return out
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            await lock_conn.commit()
