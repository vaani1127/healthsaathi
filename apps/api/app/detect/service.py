"""Detection in the product (SPEC 5.5): score today's gated accesses, raise alerts within the
daily budget, fit models each night, and let the clinic admin review alerts.

Scores only rank accesses for review. Nothing here grants or refuses access.

Scoring one clinic at a time, each access is compared with the model's calibrated threshold, and
at most ALERT_BUDGET alerts are raised per clinic and local day, highest score first. Without a
fitted model the rules baseline is used. Break-glass accesses never use the budget: they are
reviewed in their own 24-hour queue.
"""

import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, db_now, record_and_explain
from app.core.config import REPO_ROOT, get_settings
from app.core.db import TenantContext, tenant_session
from app.core.errors import ProblemError
from app.db.enums import AccessAction, AlertStatus, ResourceType
from app.db.models import (
    AccessEvent,
    AccessExplanation,
    Alert,
    AlertReview,
    Clinic,
    Patient,
    User,
)
from app.detect import schemas
from app.detect.loader import load_clinic
from app.identity.deps import ClinicPrincipal
from app.ledger.writer import append_audit_event
from app.realtime.hub import notify
from e2d_core.detect import (
    RULES,
    RULES_THRESHOLD,
    ModelBundle,
    calibrate,
    dumps,
    fit_per_clinic,
    gate,
    loads,
    model_version,
    rules_score,
)
from e2d_core.features import ALL_FEATURES, build_features

logger = logging.getLogger(__name__)

SYSTEM = "system"
FIT_DAYS = 30
TIMELINE_LENGTH = 50
GLOBAL_KEY = "global"
RULES_VERSION = model_version(json.dumps([RULES, RULES_THRESHOLD]).encode())


# Model files ----------------------------------------------------------------------------------


class ModelStore:
    """Fitted models as files named after the clinic (or "global")."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, key: str) -> Path:
        return self.root / f"{key}.model"

    def save(self, key: str, bundle: ModelBundle) -> str:
        data = dumps(bundle)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path(key).with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.path(key))
        return model_version(data)

    def load(self, key: str) -> tuple[ModelBundle, str] | None:
        path = self.path(key)
        if not path.exists():
            return None
        data = path.read_bytes()
        return loads(data), model_version(data)


def model_store() -> ModelStore:
    configured = get_settings().model_dir
    return ModelStore(Path(configured) if configured else REPO_ROOT / "data" / "models")


# Scoring --------------------------------------------------------------------------------------


def _ctx(clinic_id: uuid.UUID | None = None, db_role: str = "app_rw") -> TenantContext:
    return TenantContext(clinic_id=clinic_id, role=SYSTEM, db_role=db_role)


async def clinic_ids() -> list[uuid.UUID]:
    """Every clinic, read with the anchor_job role, which may list clinics across tenants."""
    async with tenant_session(_ctx(db_role="anchor_job")) as db:
        return list((await db.scalars(select(Clinic.id).order_by(Clinic.id))).all())


def _local_day_start(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


def _feature_values(row: dict[str, Any]) -> dict[str, float]:
    return {name: float(row.get(name) or 0.0) for name in ALL_FEATURES}


async def score_clinic(
    clinic_id: uuid.UUID, now: datetime | None = None, store: ModelStore | None = None
) -> int:
    """Score the clinic's accesses of the current local day and raise new alerts. Returns how
    many alerts were raised. Safe to run again: alerted accesses are skipped."""
    store = store or model_store()
    budget = get_settings().alert_budget
    async with tenant_session(_ctx(clinic_id)) as db:
        clinic = await db.get(Clinic, clinic_id)
        if clinic is None:
            return 0
        tz = ZoneInfo(clinic.timezone)
        now = now or await db_now(db)
        day = now.astimezone(tz).date()
        start = _local_day_start(day, tz)
        events, frames, explanations = await load_clinic(db, clinic_id, start, now)
        today = events.filter(pl.col("at") >= start)
        if today.is_empty():
            return 0
        features = build_features(events, frames, explanations).filter(
            pl.col("access_event_id").is_in(today["id"].implode())
        )

        found = store.load(str(clinic_id)) or store.load(GLOBAL_KEY)
        if found is not None:
            bundle, version = found
            scores = bundle.score(features)
            threshold = bundle.threshold if bundle.threshold is not None else 0.0
            scorer = bundle.scorer
        else:
            bundle, version = None, RULES_VERSION
            scores = rules_score(features)
            threshold, scorer = RULES_THRESHOLD, "rules"

        existing = (
            await db.scalars(
                select(Alert.access_event_id).where(Alert.clinic_id == clinic_id, Alert.day == day)
            )
        ).all()
        raised = len(existing)
        already = {str(a) for a in existing}
        scored = (
            features.with_columns(pl.Series("score", scores), gate(features).alias("gated"))
            .join(
                today.select(pl.col("id").alias("access_event_id"), "policy_version"),
                on="access_event_id",
            )
            .filter(
                pl.col("gated")
                & (pl.col("break_glass") == 0)
                & (pl.col("score") >= threshold)
                & ~pl.col("access_event_id").is_in(list(already))
            )
            .sort(["score", "access_event_id"], descending=[True, False])
        )
        created = 0
        for row in scored.head(max(0, budget - raised)).iter_rows(named=True):
            values = _feature_values(row)
            top = (
                bundle.top_features(values)
                if bundle is not None
                else [
                    {"feature": name, "value": values[name], "z": 0.0}
                    for name, at_least, _ in RULES
                    if values[name] >= at_least
                ]
            )
            db.add(
                Alert(
                    clinic_id=clinic_id,
                    access_event_id=uuid.UUID(row["access_event_id"]),
                    day=day,
                    score=float(row["score"]),
                    rank_in_day=raised + created + 1,
                    features={"values": values, "top": top, "scorer": scorer},
                    model_version=version,
                    policy_version=row["policy_version"],
                )
            )
            created += 1
        if created:
            await db.flush()
            await notify(db, clinic_id, "alerts.new", count=created)
        return created


async def score_all(now: datetime | None = None) -> dict[str, int]:
    out = {}
    for clinic_id in await clinic_ids():
        try:
            out[str(clinic_id)] = await score_clinic(clinic_id, now)
        except Exception:
            logger.exception("scoring failed", extra={"clinic_id": str(clinic_id)})
    return out


async def _training_rows(clinic_id: uuid.UUID, now: datetime) -> pl.DataFrame:
    async with tenant_session(_ctx(clinic_id)) as db:
        start = now - timedelta(days=FIT_DAYS)
        events, frames, explanations = await load_clinic(db, clinic_id, start, now)
    window = events.filter(pl.col("at") >= start)
    if window.is_empty():
        return pl.DataFrame()
    features = build_features(events, frames, explanations).filter(
        pl.col("access_event_id").is_in(window["id"].implode())
    )
    return features.filter(gate(features))


async def fit_models(
    now: datetime | None = None, scorer: str | None = None, store: ModelStore | None = None
) -> dict[str, Any]:
    """Nightly fit (SPEC 5.5): one model per clinic with enough gated accesses in the last 30
    days, and a global model for the others."""
    settings = get_settings()
    store = store or model_store()
    scorer = scorer or settings.detect_scorer
    now = now or datetime.now(UTC)
    by_clinic = {str(c): await _training_rows(c, now) for c in await clinic_ids()}
    by_clinic = {k: v for k, v in by_clinic.items() if v.height}
    models, fallback = fit_per_clinic(by_clinic, ALL_FEATURES, scorer)
    versions: dict[str, str] = {}
    for clinic, bundle in models.items():
        calibrate(bundle, by_clinic[clinic], FIT_DAYS, settings.alert_budget)
        versions[clinic] = store.save(clinic, bundle)
    if fallback is not None:
        pooled = pl.concat(list(by_clinic.values()))
        calibrate(fallback, pooled, FIT_DAYS * len(by_clinic), settings.alert_budget)
        versions[GLOBAL_KEY] = store.save(GLOBAL_KEY, fallback)
    logger.info("models fitted", extra={"scorer": scorer, "models": len(versions)})
    return {
        "scorer": scorer,
        "versions": versions,
        "rows": {k: v.height for k, v in by_clinic.items()},
    }


# Review API -----------------------------------------------------------------------------------


def _patient_ref(clinic_id: uuid.UUID, patient_id: uuid.UUID) -> str:
    return hashlib.sha256(f"{clinic_id}:{patient_id}".encode()).hexdigest()[:8]


def _top(alert: Alert) -> list[schemas.TopFeature]:
    return [schemas.TopFeature(**t) for t in alert.features.get("top", [])]


async def _alert_out(
    db: AsyncSession, alert: Alert, event: AccessEvent, user_name: str
) -> dict[str, Any]:
    explanation = await db.get(AccessExplanation, event.id)
    return {
        "id": alert.id,
        "access_event_id": alert.access_event_id,
        "day": alert.day,
        "score": alert.score,
        "rank_in_day": alert.rank_in_day,
        "status": alert.status,
        "model_version": alert.model_version,
        "policy_version": alert.policy_version,
        "scorer": str(alert.features.get("scorer", "")),
        "created_at": alert.created_at,
        "at": event.at,
        "user_id": event.user_id,
        "user_name": user_name,
        "role": event.role,
        "resource": event.resource_type,
        "action": event.action,
        "template_code": explanation.template_code if explanation else None,
        "strength": float(explanation.strength) if explanation else 0.0,
        "top_features": _top(alert),
    }


async def list_alerts(
    db: AsyncSession, clinic_id: uuid.UUID, day: date | None, status: AlertStatus | None
) -> list[schemas.AlertOut]:
    stmt = (
        select(Alert, AccessEvent, User.name)
        .join(AccessEvent, AccessEvent.id == Alert.access_event_id)
        .join(User, User.id == AccessEvent.user_id)
        .where(Alert.clinic_id == clinic_id)
    )
    if day is not None:
        stmt = stmt.where(Alert.day == day)
    if status is not None:
        stmt = stmt.where(Alert.status == status)
    rows = (await db.execute(stmt.order_by(Alert.day.desc(), Alert.rank_in_day).limit(200))).all()
    return [schemas.AlertOut(**await _alert_out(db, a, e, name)) for a, e, name in rows]


async def _get(db: AsyncSession, alert_id: uuid.UUID) -> tuple[Alert, AccessEvent, str]:
    row = (
        await db.execute(
            select(Alert, AccessEvent, User.name)
            .join(AccessEvent, AccessEvent.id == Alert.access_event_id)
            .join(User, User.id == AccessEvent.user_id)
            .where(Alert.id == alert_id)
        )
    ).first()
    if row is None:
        raise ProblemError(404, "alert-not-found")
    return row[0], row[1], row[2]


async def alert_detail(
    db: AsyncSession, principal: ClinicPrincipal, alert_id: uuid.UUID, req: RequestInfo
) -> schemas.AlertDetail:
    alert, event, user_name = await _get(db, alert_id)
    # Seeing who the patient is reads patient data, so it is recorded like any other access.
    await record_and_explain(
        db,
        principal,
        event.patient_id,
        ResourceType.DEMOGRAPHICS,
        AccessAction.VIEW,
        req,
        detail={"op": "alert_review", "alert_id": str(alert.id)},
    )
    patient = await db.get(Patient, event.patient_id)
    assert patient is not None
    explanation = await db.get(AccessExplanation, event.id)
    history = (
        await db.execute(
            select(AccessEvent, AccessExplanation.template_code, AccessExplanation.strength)
            .outerjoin(AccessExplanation, AccessExplanation.access_event_id == AccessEvent.id)
            .where(AccessEvent.user_id == event.user_id, AccessEvent.at <= event.at)
            .order_by(AccessEvent.at.desc())
            .limit(TIMELINE_LENGTH)
        )
    ).all()
    reviews = (
        await db.scalars(
            select(AlertReview).where(AlertReview.alert_id == alert.id).order_by(AlertReview.at)
        )
    ).all()
    return schemas.AlertDetail(
        **await _alert_out(db, alert, event, user_name),
        patient_id=patient.id,
        patient_name=patient.name,
        patient_mrn=patient.mrn,
        forgery_flags=explanation.forgery_flags if explanation else {},
        evidence=explanation.evidence if explanation else {},
        features=alert.features.get("values", {}),
        timeline=[
            schemas.TimelineEntry(
                at=e.at,
                role=e.role,
                resource=e.resource_type,
                action=e.action,
                decision=e.decision.value,
                template_code=code,
                strength=float(strength) if strength is not None else None,
                patient_ref=_patient_ref(e.clinic_id, e.patient_id),
                is_this_alert=e.id == event.id,
            )
            for e, code, strength in history
        ],
        reviews=[
            schemas.ReviewOut(
                reviewer_user_id=r.reviewer_user_id, outcome=r.outcome, note=r.note, at=r.at
            )
            for r in reviews
        ],
    )


async def review_alert(
    db: AsyncSession, principal: ClinicPrincipal, alert_id: uuid.UUID, body: schemas.ReviewIn
) -> Alert:
    alert, event, _ = await _get(db, alert_id)
    if event.user_id == principal.user_id:
        raise ProblemError(403, "own-alert", "Someone else must review an alert about you.")
    at = await db_now(db)
    db.add(
        AlertReview(
            clinic_id=principal.clinic_id,
            alert_id=alert.id,
            reviewer_user_id=principal.user_id,
            outcome=body.outcome,
            note=body.note,
            at=at,
        )
    )
    alert.status = AlertStatus(body.outcome.value)
    await db.flush()
    await append_audit_event(
        db,
        principal.clinic_id,
        "alert_review",
        {
            "type": "alert_review",
            "alert_id": str(alert.id),
            "access_event_id": str(alert.access_event_id),
            "reviewer_user_id": str(principal.user_id),
            "outcome": body.outcome.value,
            "model_version": alert.model_version,
            "at": at.isoformat(),
        },
    )
    return alert
