"""The daily final ranking (app.detect.daily, ADR 0008)."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app import worker
from app.core.config import get_settings
from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import AccessAction, AccessDecision, ResourceType, Role
from app.detect import daily, service, starter_rules
from app.detect.days import CATCH_UP_DAYS, DAILY_KIND, due_window, ready_after
from e2d_core.detect import gate
from e2d_core.explain import forgery
from e2d_core.features import ALL_FEATURES
from tests.helpers import Actor, make_actor, make_clinic, make_patient

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


@pytest.fixture
async def staff(admin_engine: AsyncEngine, clinic: uuid.UUID) -> dict[Role, Actor]:
    return {
        role: await make_actor(admin_engine, clinic, role)
        for role in (Role.RECEPTION, Role.DOCTOR, Role.CLINIC_ADMIN)
    }


@pytest.fixture
def store(tmp_path: Path) -> service.ModelStore:
    return service.ModelStore(tmp_path / "models")


@pytest.fixture(autouse=True)
def only_this_clinic(monkeypatch: pytest.MonkeyPatch, clinic: uuid.UUID) -> None:
    """Other tests' clinics share the database; the pooled model must not see them."""

    async def ids() -> list[uuid.UUID]:
        return [clinic]

    monkeypatch.setattr(daily, "clinic_ids", ids)
    daily.FailureState.retry_at.clear()


async def open_with_reason(client: AsyncClient, doctor: Actor, patient: uuid.UUID) -> None:
    r = await client.post(
        f"/api/v1/patients/{patient}/chart",
        json={"reason_code": "covering_doctor", "reason_text": "Covering for a colleague"},
        headers=doctor.headers,
    )
    assert r.status_code == 200, r.text


async def latest_access(engine: AsyncEngine, clinic: uuid.UUID) -> datetime:
    async with AsyncSession(engine) as s:
        at = await s.scalar(
            select(func.max(m.AccessEvent.at)).where(m.AccessEvent.clinic_id == clinic)
        )
    assert at is not None
    return at


async def provisional_day(
    client: AsyncClient,
    engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
    patients: int = 3,
) -> date:
    """Unexplained chart openings on several patients, scored by the per-minute scorer. Returns
    the clinic day they fall on."""
    for _ in range(patients):
        patient = await make_patient(engine, clinic, staff[Role.RECEPTION].user_id)
        await open_with_reason(client, staff[Role.DOCTOR], patient)
    latest = await latest_access(engine, clinic)
    await service.score_clinic(clinic, now=latest + timedelta(microseconds=1), store=store)
    return latest.astimezone(IST).date()


async def alerts_of(engine: AsyncEngine, clinic: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
    """Every alert of the clinic as plain values, to compare before and after."""
    async with AsyncSession(engine) as s:
        rows = (await s.scalars(select(m.Alert).where(m.Alert.clinic_id == clinic))).all()
    return {
        a.access_event_id: {
            "id": a.id,
            "day": a.day,
            "score": a.score,
            "rank_in_day": a.rank_in_day,
            "features": a.features,
            "model_version": a.model_version,
            "policy_version": a.policy_version,
            "status": a.status,
        }
        for a in rows
    }


async def daily_entries(engine: AsyncEngine, clinic: uuid.UUID) -> list[dict[str, Any]]:
    async with AsyncSession(engine) as s:
        rows = await s.scalars(
            select(m.AuditEvent.payload)
            .where(m.AuditEvent.clinic_id == clinic, m.AuditEvent.kind == DAILY_KIND)
            .order_by(m.AuditEvent.seq)
        )
        return list(rows.all())


def due(day: date) -> datetime:
    return ready_after(day, IST) + timedelta(minutes=1)


def stage(alert: dict[str, Any]) -> str:
    return str((alert["features"].get("lifecycle") or {}).get("stage", "provisional"))


# Timing ---------------------------------------------------------------------------------------


async def test_a_day_is_ready_at_midnight_two_days_later_in_clinic_time() -> None:
    expected = datetime.fromisoformat("2026-11-14T18:30:00+00:00")
    assert ready_after(date(2026, 11, 13), IST) == expected
    window = due_window(date(2026, 11, 20))
    assert window[0] == date(2026, 11, 12) and window[-1] == date(2026, 11, 18)
    assert len(window) == CATCH_UP_DAYS


async def test_a_day_that_is_not_ready_is_refused(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=1)
    with pytest.raises(ValueError, match="not ready"):
        await daily.finalise_day(
            clinic, day, now=ready_after(day, IST) - timedelta(seconds=1), run=daily.new_run(store)
        )
    assert await daily_entries(admin_engine, clinic) == []


# The final list -------------------------------------------------------------------------------


async def test_final_list_adds_finalises_and_retires_and_is_audited(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store)
    before = await alerts_of(admin_engine, clinic)
    assert len(before) >= 3 and {stage(a) for a in before.values()} == {"provisional"}

    result = await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store, b=1))
    assert result is not None
    after = await alerts_of(admin_engine, clinic)
    final = [k for k, a in after.items() if stage(a) == "final"]
    retired = [k for k, a in after.items() if stage(a) == "retired"]
    assert len(final) == 1 and len(retired) == len(after) - 1

    [entry] = await daily_entries(admin_engine, clinic)
    assert entry["outcome"] == "finalised" and entry["day"] == day.isoformat()
    assert entry["final"] == [str(final[0])]
    assert entry["added_count"] + len(entry["finalised"]) == 1
    assert entry["retired_count"] == len(retired) and sorted(entry["retired"]) == sorted(
        str(r) for r in retired
    )
    assert entry["model_version"] == starter_rules.VERSION and entry["scorer"] == "starter_rules"
    assert entry["as_of_rule"] == daily.AS_OF_RULE
    assert len(entry["cache_sha256"]) == 64
    assert entry["late_flags_undecided"] == 0
    winner = after[final[0]]
    assert winner["model_version"] == entry["model_version"]
    # A typed reason is not evidence the late flags look at.
    assert winner["features"]["late_flags"]["status"] == "not_applicable"
    assert entry["late_flags_status"] == {"not_applicable": entry["staff_accesses"]}
    assert set(winner["features"]["values"]) == set(ALL_FEATURES), "model inputs stay clean"

    admin = staff[Role.CLINIC_ADMIN]
    listed = (await client.get("/api/v1/alerts", headers=admin.headers)).json()
    assert [a["list_state"] for a in listed] == ["final"] and listed[0]["day_final"]
    everything = (
        await client.get("/api/v1/alerts?include_retired=true", headers=admin.headers)
    ).json()
    assert sorted(a["list_state"] for a in everything) == ["final"] + ["retired"] * len(retired)
    open_ = (
        await client.get("/api/v1/alerts?status=open&include_retired=true", headers=admin.headers)
    ).json()
    assert [a["list_state"] for a in open_] == ["final"], "retired alerts are never open"
    status = (await client.get(f"/api/v1/alerts/days/{day}", headers=admin.headers)).json()
    assert status["state"] == "final" and status["final_count"] == 1
    assert status["ready_after"].startswith(ready_after(day, IST).isoformat()[:19])


async def test_running_the_job_twice_changes_nothing(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store)
    assert await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store, b=1))
    alerts = await alerts_of(admin_engine, clinic)
    entries = await daily_entries(admin_engine, clinic)
    cache = sorted((store.root / "daily").glob("*.parquet"))
    cache_bytes = [p.read_bytes() for p in cache]

    again = await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store, b=1))
    assert again is None
    first = await daily.run_due(now=due(day), store=store)
    second = await daily.run_due(now=due(day), store=store)
    assert first is not None and second == {str(clinic): []}
    assert await alerts_of(admin_engine, clinic) == alerts
    assert len(await daily_entries(admin_engine, clinic)) == len(entries) + len(
        first[str(clinic)]
    ), "the second run adds no audit entry"
    assert {r.day for r in first[str(clinic)]}.isdisjoint({day}), "the finalised day is not redone"
    assert [p.read_bytes() for p in cache] == cache_bytes


async def test_a_reviewed_provisional_alert_survives_the_daily_job(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store)
    admin = staff[Role.CLINIC_ADMIN]
    reviewed = []
    for alert in (await alerts_of(admin_engine, clinic)).values():
        r = await client.post(
            f"/api/v1/alerts/{alert['id']}/review",
            json={"outcome": "benign", "note": "Covering that day"},
            headers=admin.headers,
        )
        assert r.status_code == 200, r.text
        reviewed.append(alert["id"])
    before = await alerts_of(admin_engine, clinic)

    assert await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store, b=1))
    after = await alerts_of(admin_engine, clinic)
    for key, alert in before.items():
        assert after[key] == alert, "a reviewed alert is never changed"
    [entry] = await daily_entries(admin_engine, clinic)
    kept = set(entry["reviewed_in_final"]) | set(entry["reviewed_outside_final"])
    assert kept == {str(k) for k in before}
    assert entry["retired"] == [] and entry["finalised"] == []
    assert len(entry["reviewed_outside_final"]) >= len(before) - 1

    states = {
        a["id"]: a["list_state"]
        for a in (await client.get(f"/api/v1/alerts?day={day}", headers=admin.headers)).json()
    }
    assert {states[str(i)] for i in reviewed} <= {"final", "outside_final"}
    assert "outside_final" in states.values()


async def test_a_crash_before_commit_leaves_the_day_to_be_redone(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store)
    before = await alerts_of(admin_engine, clinic)

    def crash() -> None:
        raise RuntimeError("worker killed")

    with pytest.raises(RuntimeError, match="worker killed"):
        await daily.finalise_day(
            clinic, day, now=due(day), run=daily.new_run(store, b=1), before_commit=crash
        )
    assert await alerts_of(admin_engine, clinic) == before, "no alert changed"
    assert await daily_entries(admin_engine, clinic) == [], "the day is not finalised"

    result = await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store, b=1))
    assert result is not None and result.payload["outcome"] == "finalised"
    assert len(await daily_entries(admin_engine, clinic)) == 1


async def test_days_older_than_the_catch_up_window_get_a_skipped_entry(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=1)
    now = due(day + timedelta(days=CATCH_UP_DAYS + 3))
    runs = await daily.run_due(now=now, store=store)
    assert runs is not None
    results = runs[str(clinic)]
    skipped = [r for r in results if r.outcome == "skipped"]
    assert [r.day for r in skipped] == [day]
    assert skipped[0].payload["reason"] == daily.SKIP_REASON
    finalised = [r for r in results if r.outcome == "finalised"]
    assert len(finalised) == CATCH_UP_DAYS and all(
        r.payload["staff_accesses"] == 0 for r in finalised
    )
    assert await daily.run_due(now=now, store=store) == {str(clinic): []}
    admin = staff[Role.CLINIC_ADMIN]
    status = (await client.get(f"/api/v1/alerts/days/{day}", headers=admin.headers)).json()
    assert status["state"] == "skipped"


async def booked_visit_then_cancelled(
    client: AsyncClient, engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> tuple[date, list[str]]:
    """Reception books an appointment, the doctor opens the chart (explained by the
    appointment), then reception cancels it. Returns the day and the doctor's access ids."""
    patient = await make_patient(engine, clinic, staff[Role.RECEPTION].user_id)
    slot = datetime.now(IST) + timedelta(minutes=10)
    r = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": str(patient),
            "doctor_user_id": str(staff[Role.DOCTOR].user_id),
            "slot_start": slot.isoformat(),
        },
        headers=staff[Role.RECEPTION].headers,
    )
    assert r.status_code == 201, r.text
    appointment = r.json()["id"]
    r = await client.get(f"/api/v1/patients/{patient}/chart", headers=staff[Role.DOCTOR].headers)
    assert r.status_code == 200, r.text
    r = await client.patch(
        f"/api/v1/appointments/{appointment}",
        json={"status": "cancelled"},
        headers=staff[Role.RECEPTION].headers,
    )
    assert r.status_code == 200, r.text
    async with AsyncSession(engine) as s:
        rows = (
            await s.execute(
                select(m.AccessEvent.id, m.AccessEvent.at, m.AccessExplanation.template_code)
                .join(m.AccessExplanation, m.AccessExplanation.access_event_id == m.AccessEvent.id)
                .where(
                    m.AccessEvent.clinic_id == clinic,
                    m.AccessEvent.user_id == staff[Role.DOCTOR].user_id,
                )
            )
        ).all()
    assert rows and {r.template_code for r in rows} == {"T_APPT"}
    return rows[0].at.astimezone(IST).date(), sorted(str(r.id) for r in rows)


async def late_of(clinic: uuid.UUID, day: date) -> dict[str, daily.LateResult]:
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        return (await daily.day_features(db, clinic, day, "Asia/Kolkata")).late


async def test_late_flags_are_computed_for_the_stored_evidence(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day, doctor = await booked_visit_then_cancelled(client, admin_engine, clinic, staff)
    late = await late_of(clinic, day)
    for event_id in doctor:
        assert late[event_id].status == "decided"
        assert late[event_id].flags["cancelled_after_access"] is True, (
            "the appointment that explained the access was cancelled after it"
        )
    result = await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store))
    assert result is not None and result.payload["late_flags_undecided"] == 0


async def test_no_progress_is_set_when_nothing_follows_the_booked_visit(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    """An appointment explains the doctor's access, and in the next 24 hours the visit never
    moves forward and the patient gets no vitals, note, prescription, result or invoice."""
    patient = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    r = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": str(patient),
            "doctor_user_id": str(staff[Role.DOCTOR].user_id),
            "slot_start": (datetime.now(IST) + timedelta(minutes=10)).isoformat(),
        },
        headers=staff[Role.RECEPTION].headers,
    )
    assert r.status_code == 201, r.text
    r = await client.get(f"/api/v1/patients/{patient}/chart", headers=staff[Role.DOCTOR].headers)
    assert r.status_code == 200, r.text
    async with AsyncSession(admin_engine) as s:
        doctor = [
            (str(i), at)
            for i, at, code in (
                await s.execute(
                    select(m.AccessEvent.id, m.AccessEvent.at, m.AccessExplanation.template_code)
                    .join(
                        m.AccessExplanation,
                        m.AccessExplanation.access_event_id == m.AccessEvent.id,
                    )
                    .where(
                        m.AccessEvent.clinic_id == clinic,
                        m.AccessEvent.user_id == staff[Role.DOCTOR].user_id,
                    )
                )
            ).all()
            if code == "T_APPT"
        ]
    assert doctor
    day = doctor[0][1].astimezone(IST).date()
    late = await late_of(clinic, day)
    for event_id, _ in doctor:
        assert late[event_id].status == "decided"
        assert late[event_id].flags == {"no_progress": True, "cancelled_after_access": False}
    result = await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store))
    assert result is not None and result.payload["late_flags_undecided"] == 0


async def test_a_missing_evidence_record_leaves_late_flags_undecided(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day, doctor = await booked_visit_then_cancelled(client, admin_engine, clinic, staff)
    monkeypatch.setattr(forgery, "_find", lambda *args: None)
    late = await late_of(clinic, day)
    for event_id in doctor:
        assert late[event_id] == daily.LateResult("undecided", daily.UNDECIDED)
    result = await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store))
    assert result is not None
    payload = result.payload
    assert set(doctor) <= set(payload["undecided_access_event_ids"])
    assert payload["late_flags_undecided"] == len(payload["undecided_access_event_ids"])
    for alert in (await alerts_of(admin_engine, clinic)).values():
        late_flags = alert["features"].get("late_flags") or {}
        if stage(alert) == "final" and late_flags.get("status") == "undecided":
            assert alert["features"]["late_flags"] == {
                "status": "undecided",
                "no_progress": None,
                "cancelled_after_access": None,
            }
            assert alert["features"]["values"]["flag_cancelled_after_access"] == 0.0


async def test_only_admins_of_the_clinic_see_day_status(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
) -> None:
    today = date.today().isoformat()
    for role in (Role.DOCTOR, Role.RECEPTION):
        r = await client.get(f"/api/v1/alerts/days/{today}", headers=staff[role].headers)
        assert r.status_code == 403
    r = await client.get(f"/api/v1/alerts/days/{today}", headers=staff[Role.CLINIC_ADMIN].headers)
    assert r.status_code == 200 and r.json()["state"] == "provisional"


async def test_another_clinics_final_day_is_not_visible(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=1)
    assert await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store))
    other = await make_actor(admin_engine, await make_clinic(admin_engine), Role.CLINIC_ADMIN)
    status = (await client.get(f"/api/v1/alerts/days/{day}", headers=other.headers)).json()
    assert status["state"] == "provisional", "clinic B does not see clinic A's daily entry"
    listed = await client.get("/api/v1/alerts?include_retired=true", headers=other.headers)
    assert listed.json() == []


async def test_a_missing_or_corrupt_cache_is_rebuilt(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=1)
    run = daily.new_run(store)
    rows = await run.training_rows(clinic, [day])
    cache = run.cache(clinic)
    good = cache.path.read_bytes()
    cache.path.write_bytes(b"not parquet")
    with caplog.at_level("WARNING"):
        rebuilt = daily.new_run(store)
        again = await rebuilt.training_rows(clinic, [day])
    assert "feature cache unreadable" in caplog.text
    assert again.equals(rows)
    assert rebuilt.cache(clinic).path.read_bytes() == good


async def test_worker_runs_the_daily_job_after_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = service.ModelStore(tmp_path / "m")
    calls: list[str] = []

    async def fake(name: str) -> Any:
        calls.append(name)
        return {}

    monkeypatch.setattr(service, "model_store", lambda: store)
    monkeypatch.setattr(service, "fit_models", lambda **_: fake("fit"))
    monkeypatch.setattr(service, "score_all", lambda **_: fake("score"))
    monkeypatch.setattr(daily, "run_due", lambda **_: fake("daily"))
    await worker.detect_tick()
    assert calls == ["fit", "score", "daily"]


async def test_break_glass_never_enters_the_final_list_or_uses_the_budget(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gate() lets break-glass accesses through; budget() is what keeps them out of the list."""
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=2)
    patient = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    r = await client.post(
        f"/api/v1/patients/{patient}/break-glass",
        json={"reason_code": "unconscious", "reason_text": "Collapsed at reception"},
        headers=staff[Role.DOCTOR].headers,
    )
    assert r.status_code == 201, r.text
    async with AsyncSession(admin_engine) as s:
        glass = {
            str(i)
            for i in (
                await s.scalars(
                    select(m.AccessEvent.id).where(
                        m.AccessEvent.clinic_id == clinic,
                        m.AccessEvent.break_glass_id.is_not(None),
                    )
                )
            ).all()
        }
    assert glass

    def highest_for_break_glass(rows: pl.DataFrame) -> np.ndarray:
        return np.asarray(1.0 + 100.0 * rows["break_glass"].to_numpy(), dtype=float)

    monkeypatch.setattr(starter_rules, "score", highest_for_break_glass)
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        rows = (await daily.day_features(db, clinic, day, "Asia/Kolkata")).rows
    on_glass = rows.filter(pl.col("access_event_id").is_in(list(glass)))
    assert on_glass.height == len(glass) and gate(on_glass).all(), "break-glass passes the gate"

    result = await daily.finalise_day(clinic, day, now=due(day), run=daily.new_run(store, b=1))
    assert result is not None
    final = result.payload["final"]
    assert len(final) == 1, "the one budget slot went to an access that is not break-glass"
    assert not set(final) & glass
    after = await alerts_of(admin_engine, clinic)
    assert not {str(k) for k in after} & glass, "no alert of any stage for a break-glass access"


async def finalised_days(engine: AsyncEngine, clinic: uuid.UUID) -> set[str]:
    return {p["day"] for p in await daily_entries(engine, clinic) if p["outcome"] == "finalised"}


async def test_a_wider_catch_up_window_finalises_older_days(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=1)
    now = due(day + timedelta(days=CATCH_UP_DAYS + 3))
    runs = await daily.run_due(now=now, store=store, catch_up=CATCH_UP_DAYS + 5)
    assert runs is not None
    assert [r for r in runs[str(clinic)] if r.outcome == "skipped"] == []
    assert day.isoformat() in await finalised_days(admin_engine, clinic)


async def test_a_capped_run_computes_a_few_training_days_and_continues_later(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=1)
    cached: list[int] = []
    for _ in range(3):
        before = len(daily.FeatureCache(store.root, clinic).days())
        assert await daily.run_due(now=due(day), store=store, max_cache_days=10) is not None
        cached.append(len(daily.FeatureCache(store.root, clinic).days()) - before)
        if day.isoformat() in await finalised_days(admin_engine, clinic):
            break
    # 30 training days are needed; each run computes at most 10 of them (plus the days it
    # finalises, which go into the cache too).
    assert day.isoformat() in await finalised_days(admin_engine, clinic)
    assert len(cached) == 3, "the day waited for its training features"
    assert all(n <= 10 + CATCH_UP_DAYS + 1 for n in cached)


async def test_a_capped_run_finalises_one_clinic_day_with_accesses(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    other = await make_clinic(admin_engine)
    other_staff = {
        role: await make_actor(admin_engine, other, role)
        for role in (Role.RECEPTION, Role.DOCTOR, Role.CLINIC_ADMIN)
    }
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=1)
    other_day = await provisional_day(client, admin_engine, other, other_staff, store, patients=1)
    assert other_day == day
    clinics = [clinic, other]
    first = await daily.run_due(now=due(day), clinics=clinics, store=store, max_days=1)
    assert first is not None
    assert day.isoformat() in await finalised_days(admin_engine, clinic)
    assert await finalised_days(admin_engine, other) == set(), "the second clinic waits"
    second = await daily.run_due(now=due(day), clinics=clinics, store=store, max_days=1)
    assert second is not None and second[str(clinic)] == []
    assert day.isoformat() in await finalised_days(admin_engine, other)


# Days before the clinic's first staff access -----------------------------------------------------


async def test_a_wide_catch_up_starts_at_the_first_staff_access_day(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=1)
    now = due(day + timedelta(days=3))
    runs = await daily.run_due(now=now, store=store, catch_up=400)
    assert runs is not None
    results = runs[str(clinic)]
    assert [r.day for r in results] == [day + timedelta(days=k) for k in range(4)]
    assert all(r.outcome == "finalised" for r in results)
    assert results[0].payload["staff_accesses"] > 0
    written = [p["day"] for p in await daily_entries(admin_engine, clinic)]
    assert min(written) == day.isoformat() and len(written) == 4


async def test_the_worker_tick_starts_at_the_first_staff_access_day(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's own call, with its caps and DETECT_CATCH_UP_DAYS=400, as on 25 Nov 2026."""
    day = await provisional_day(client, admin_engine, clinic, staff, store, patients=1)
    now = due(day + timedelta(days=3))
    monkeypatch.setattr(get_settings(), "detect_catch_up_days", 400)
    for _ in range(40):
        runs = await daily.run_due(
            now=now,
            store=store,
            max_days=worker.DAILY_DAYS_PER_TICK,
            max_cache_days=worker.DAILY_CACHE_DAYS_PER_TICK,
        )
        assert runs is not None
        assert all(r.day >= day for r in runs[str(clinic)])
        if len(await daily_entries(admin_engine, clinic)) >= 4:
            break
    entries = await daily_entries(admin_engine, clinic)
    assert [p["day"] for p in entries] == [(day + timedelta(days=k)).isoformat() for k in range(4)]
    assert all(p["outcome"] == "finalised" for p in entries)
    assert await daily.run_due(now=now, store=store) == {str(clinic): []}


async def test_nothing_is_due_for_a_clinic_with_no_staff_access(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    store: service.ModelStore,
) -> None:
    """Another clinic's accesses and the clinic's own patient accesses do not count."""
    other = await make_clinic(admin_engine)
    other_staff = {
        role: await make_actor(admin_engine, other, role) for role in (Role.RECEPTION, Role.DOCTOR)
    }
    day = await provisional_day(client, admin_engine, other, other_staff, store, patients=1)
    patient = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    async with AsyncSession(admin_engine) as s, s.begin():
        s.add(
            m.AccessEvent(
                clinic_id=clinic,
                user_id=staff[Role.DOCTOR].user_id,
                role=Role.PATIENT,
                patient_id=patient,
                resource_type=ResourceType.DEMOGRAPHICS,
                action=AccessAction.VIEW,
                policy_version="test",
                decision=AccessDecision.ALLOW,
            )
        )
    for catch_up in (None, 400):
        runs = await daily.run_due(now=due(day), store=store, catch_up=catch_up)
        assert runs == {str(clinic): []}
    assert await daily_entries(admin_engine, clinic) == []


async def test_the_first_access_day_is_the_clinics_local_day(
    admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    """01:30 in India is 20:00 UTC the day before: the local day counts, not the UTC day."""
    local = datetime.now(IST).date()
    if local.day == 1:  # 01:30 on the 1st is last month in UTC, which may have no partition
        local += timedelta(days=1)
    at = datetime.combine(local, time(1, 30), tzinfo=IST)
    assert at.astimezone(UTC).date() == local - timedelta(days=1)
    patient = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    async with AsyncSession(admin_engine) as s, s.begin():
        s.add(
            m.AccessEvent(
                at=at,
                clinic_id=clinic,
                user_id=staff[Role.DOCTOR].user_id,
                role=Role.DOCTOR,
                patient_id=patient,
                resource_type=ResourceType.DEMOGRAPHICS,
                action=AccessAction.VIEW,
                policy_version="test",
                decision=AccessDecision.ALLOW,
            )
        )
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        assert await daily.first_staff_day(db, clinic, "Asia/Kolkata") == local
