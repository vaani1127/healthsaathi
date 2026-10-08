"""Record and explain every access to patient data (SPEC 4 and 5).

`record_and_explain()` must be called by every service that reads or writes patient data, inside
the same transaction as that read or write. In that transaction it:

1. decides with the deterministic policy (policy.yaml + the patient-self rule),
2. explains the access from workflow evidence with the e2d-core engine (SQL repository, same
   snapshot and RLS context as the read),
3. writes the access_event, its access_explanation and the audit_event.

A denied access raises `AccessDeniedError`. `access_session()`, used by the request dependency,
catches it after the caller's transaction has rolled back and writes the denial in a new
transaction, so the refusal is always logged and never waits on locks held by the failed one.
"""

import hashlib
import hmac
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.policy import Policy, Requirement, get_policy
from app.core.config import get_settings
from app.core.db import tenant_session
from app.core.errors import ProblemError
from app.db.enums import AccessAction, AccessDecision, ResourceType, Role
from app.db.models import (
    AccessEvent,
    AccessExplanation,
    BreakGlassEvent,
    Clinic,
    Patient,
    UserSession,
)
from app.identity.deps import ClinicPrincipal
from app.ledger.writer import append_audit_event
from e2d_core.explain import AccessEvent as CoreAccessEvent
from e2d_core.explain import EvidenceQuery, Explanation, default_config, explain
from e2d_core.explain.model import AccessReason
from e2d_core.ids import uuid7
from e2d_core.repo.sql import SqlRepository


@dataclass(frozen=True)
class RequestInfo:
    request_id: str | None = None
    client_ip: str | None = None


@dataclass(frozen=True)
class SessionRef:
    session_id: uuid.UUID | None
    device_id: uuid.UUID | None


async def current_session(db: AsyncSession, family: uuid.UUID) -> SessionRef:
    """The live session row (it changes on every refresh) and its device."""
    row = (
        await db.execute(
            select(UserSession.id, UserSession.device_id)
            .where(UserSession.family_id == family, UserSession.revoked_at.is_(None))
            .order_by(UserSession.created_at.desc())
            .limit(1)
        )
    ).first()
    return SessionRef(row.id, row.device_id) if row else SessionRef(None, None)


@dataclass(frozen=True)
class AccessRecord:
    access_event_id: uuid.UUID
    patient_id: uuid.UUID
    requirement: Requirement
    explanation: Explanation


def _ip_hash(ip: str | None) -> str | None:
    if not ip:
        return None
    key = get_settings().otp_hmac_key.get_secret_value().encode()
    return hmac.new(key, b"ip:" + ip.encode(), hashlib.sha256).hexdigest()


async def db_now(db: AsyncSession) -> datetime:
    """Access times come from the database clock, the same clock that stamps workflow rows, so
    evidence created earlier can never look newer than the access because of clock skew."""
    now = await db.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(now, datetime)
    return now


async def clinic_timezone(db: AsyncSession, clinic_id: uuid.UUID) -> str:
    tz = await db.scalar(select(Clinic.timezone).where(Clinic.id == clinic_id))
    return tz or "Asia/Kolkata"


def _audit_payload(event: AccessEvent, explanation: Explanation | None) -> dict[str, object]:
    return {
        "type": "access",
        "access_event_id": str(event.id),
        "at": event.at.isoformat(),
        "user_id": str(event.user_id),
        "role": event.role.value,
        "patient_id": str(event.patient_id),
        "resource": event.resource_type.value,
        "action": event.action.value,
        "decision": event.decision.value,
        "policy_version": event.policy_version,
        "template": explanation.template_code if explanation else None,
        "strength": round(explanation.strength, 6) if explanation else 0,
    }


async def _write(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    resource: ResourceType,
    action: AccessAction,
    decision: AccessDecision,
    explanation: Explanation,
    policy: Policy,
    request: RequestInfo,
    at: datetime,
    break_glass_id: uuid.UUID | None,
) -> AccessEvent:
    session = await current_session(db, principal.session_id)
    event = AccessEvent(
        id=uuid7(),
        at=at,
        clinic_id=principal.clinic_id,
        user_id=principal.user_id,
        role=principal.role,
        patient_id=patient_id,
        resource_type=resource,
        action=action,
        session_id=session.session_id,
        device_id=session.device_id,
        ip_hash=_ip_hash(request.client_ip),
        request_id=request.request_id,
        policy_version=policy.sha256,
        decision=decision,
        break_glass_id=break_glass_id,
    )
    db.add(event)
    db.add(
        AccessExplanation(
            access_event_id=event.id,
            clinic_id=principal.clinic_id,
            template_code=explanation.template_code,
            evidence=explanation.evidence_json(),
            strength=explanation.strength,
            forgery_flags=explanation.forgery_flags,
        )
    )
    await db.flush()
    await append_audit_event(db, principal.clinic_id, "access", _audit_payload(event, explanation))
    return event


@dataclass(frozen=True)
class Denial:
    principal: ClinicPrincipal
    patient_id: uuid.UUID
    resource: ResourceType
    action: AccessAction
    request: RequestInfo
    break_glass_id: uuid.UUID | None


class AccessDeniedError(ProblemError):
    def __init__(
        self,
        denial: Denial,
        status: int = 403,
        code: str = "access-denied",
        detail: str = "Your role cannot open this part of the record.",
    ) -> None:
        super().__init__(status, code, detail)
        self.denial = denial


BREAK_GLASS_WINDOW = timedelta(hours=4)


async def _valid_break_glass(
    db: AsyncSession, principal: ClinicPrincipal, patient_id: uuid.UUID, break_glass_id: uuid.UUID
) -> bool:
    row = await db.get(BreakGlassEvent, break_glass_id)
    if row is None or row.user_id != principal.user_id or row.patient_id != patient_id:
        return False
    age: timedelta = await db_now(db) - row.at
    return age <= BREAK_GLASS_WINDOW


async def log_denial(denial: Denial) -> None:
    async with tenant_session(denial.principal.tenant()) as db:
        await _write(
            db,
            denial.principal,
            denial.patient_id,
            denial.resource,
            denial.action,
            AccessDecision.DENY,
            Explanation(template_code=None, strength=0.0),
            get_policy(),
            denial.request,
            await db_now(db),
            denial.break_glass_id,
        )


@asynccontextmanager
async def access_session(principal: ClinicPrincipal) -> AsyncIterator[AsyncSession]:
    """A tenant session that logs any access denial after its own transaction rolls back."""
    try:
        async with tenant_session(principal.tenant()) as db:
            yield db
    except AccessDeniedError as exc:
        await log_denial(exc.denial)
        raise


async def record_and_explain(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    resource: ResourceType,
    action: AccessAction,
    request: RequestInfo | None = None,
    *,
    break_glass_id: uuid.UUID | None = None,
    reason: AccessReason | None = None,
    require_reason: bool = False,
) -> AccessRecord:
    """Authorise, explain and log one access. Raises 404 or 403 (after logging the denial).

    With `require_reason`, a doctor whose access is explained below theta and who gave no reason
    gets 428 (the attempt is logged as a denial) and must retry with a typed reason (T_REASON).
    """
    request = request or RequestInfo()
    policy = get_policy()
    row = (
        await db.execute(
            select(Patient.user_id).where(
                Patient.id == patient_id, Patient.clinic_id == principal.clinic_id
            )
        )
    ).first()
    if row is None:
        raise ProblemError(404, "patient-not-found")

    if break_glass_id is not None and not await _valid_break_glass(
        db, principal, patient_id, break_glass_id
    ):
        raise ProblemError(403, "break-glass-expired", "Start a new break-glass access.")
    decision = policy.decide(
        principal.role, resource, action, break_glass=break_glass_id is not None
    )
    allowed = decision.allowed
    if principal.role == Role.PATIENT and row.user_id != principal.user_id:
        allowed = False  # A patient account may only open its own record.
    if not allowed:
        raise AccessDeniedError(
            Denial(principal, patient_id, resource, action, request, break_glass_id)
        )

    at = await db_now(db)
    core_event = CoreAccessEvent(
        id=uuid7(),
        clinic_id=principal.clinic_id,
        user_id=principal.user_id,
        role=principal.role.value,
        patient_id=patient_id,
        resource=resource.value,
        action=action.value,
        at=at,
        reason=reason,
        break_glass_id=break_glass_id,
    )
    timezone = await clinic_timezone(db, principal.clinic_id)
    bundle = await SqlRepository(db).evidence_for(
        EvidenceQuery(
            principal.clinic_id,
            principal.user_id,
            patient_id,
            at,
            timezone,
            role=principal.role.value,
        )
    )
    explanation = explain(core_event, bundle, timezone=timezone)
    needs_reason = (
        require_reason
        and principal.role == Role.DOCTOR
        and reason is None
        and break_glass_id is None
        and decision.requirement == Requirement.EXPLANATION_OR_REVIEW
        and explanation.strength < default_config().theta
    )
    if needs_reason:
        raise AccessDeniedError(
            Denial(principal, patient_id, resource, action, request, break_glass_id),
            status=428,
            code="reason-required",
            detail="Tell us why you need this record. The reason is saved with the access.",
        )
    event = await _write(
        db,
        principal,
        patient_id,
        resource,
        action,
        AccessDecision.ALLOW,
        explanation,
        policy,
        request,
        at,
        break_glass_id,
    )
    return AccessRecord(event.id, patient_id, decision.requirement, explanation)


async def record_many(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_ids: Iterable[uuid.UUID],
    resource: ResourceType,
    action: AccessAction,
    request: RequestInfo | None = None,
) -> Sequence[AccessRecord]:
    """One access event per patient, for list screens that show several patients."""
    seen: set[uuid.UUID] = set()
    records = []
    for patient_id in patient_ids:
        if patient_id in seen:
            continue
        seen.add(patient_id)
        records.append(
            await record_and_explain(db, principal, patient_id, resource, action, request)
        )
    return records
