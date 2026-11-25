import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app import worker
from app.core.config import get_settings
from app.db import models as m
from app.db.enums import Role
from app.detect import daily, service, starter_rules
from tests.helpers import Actor, make_actor, make_clinic, make_patient


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
async def patient(
    admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> uuid.UUID:
    return await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)


@pytest.fixture
def store(tmp_path: Path) -> service.ModelStore:
    return service.ModelStore(tmp_path / "models")


async def open_with_reason(client: AsyncClient, doctor: Actor, patient: uuid.UUID) -> None:
    r = await client.post(
        f"/api/v1/patients/{patient}/chart",
        json={"reason_code": "covering_doctor", "reason_text": "Covering for a colleague"},
        headers=doctor.headers,
    )
    assert r.status_code == 200, r.text


async def score(engine: AsyncEngine, clinic: uuid.UUID, store: service.ModelStore) -> int:
    """Score the clinic as of just after its latest access, so the scored day is the day of that
    access whatever the real date (scoring after midnight would otherwise look at the next day)."""
    async with AsyncSession(engine) as s:
        latest = await s.scalar(
            select(func.max(m.AccessEvent.at)).where(m.AccessEvent.clinic_id == clinic)
        )
    assert latest is not None
    return await service.score_clinic(clinic, now=latest + timedelta(microseconds=1), store=store)


async def alerts_of(engine: AsyncEngine, clinic: uuid.UUID) -> list[m.Alert]:
    async with AsyncSession(engine) as s:
        return list((await s.scalars(select(m.Alert).where(m.Alert.clinic_id == clinic))).all())


async def test_starter_rules_raise_provisional_alerts_within_the_budget(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
    store: service.ModelStore,
) -> None:
    await open_with_reason(client, staff[Role.DOCTOR], patient)
    created = await score(admin_engine, clinic, store)
    budget = get_settings().alert_budget
    alerts = await alerts_of(admin_engine, clinic)
    assert 0 < created == len(alerts) <= budget
    assert sorted(a.rank_in_day for a in alerts) == list(range(1, created + 1))
    first = min(alerts, key=lambda a: a.rank_in_day)
    assert first.model_version == starter_rules.VERSION
    assert first.features["scorer"] == "starter_rules"
    assert first.features["lifecycle"] == {"stage": "provisional"}
    assert first.score >= starter_rules.THRESHOLD
    assert len(first.policy_version) == 64

    # A second run raises nothing new for the same accesses.
    assert await score(admin_engine, clinic, store) == 0

    # More unexplained accesses never take the day past the budget.
    for _ in range(3):
        await open_with_reason(client, staff[Role.DOCTOR], patient)
    await score(admin_engine, clinic, store)
    assert len(await alerts_of(admin_engine, clinic)) == budget


async def test_break_glass_is_not_in_the_budget(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
    store: service.ModelStore,
) -> None:
    r = await client.post(
        f"/api/v1/patients/{patient}/break-glass",
        json={"reason_code": "unconscious", "reason_text": "Collapsed at reception"},
        headers=staff[Role.DOCTOR].headers,
    )
    assert r.status_code == 201, r.text
    assert await score(admin_engine, clinic, store) == 0
    assert await alerts_of(admin_engine, clinic) == []


async def test_empty_or_unknown_clinic_scores_nothing(
    admin_engine: AsyncEngine, store: service.ModelStore
) -> None:
    assert await service.score_clinic(await make_clinic(admin_engine), store=store) == 0
    assert await service.score_clinic(uuid.uuid4(), store=store) == 0


async def test_alert_queue_detail_and_review(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
    store: service.ModelStore,
) -> None:
    await open_with_reason(client, staff[Role.DOCTOR], patient)
    await score(admin_engine, clinic, store)
    admin = staff[Role.CLINIC_ADMIN]

    listed = await client.get("/api/v1/alerts", headers=admin.headers)
    assert listed.status_code == 200
    items = listed.json()
    assert items and items[0]["rank_in_day"] == 1
    assert items[0]["user_id"] == str(staff[Role.DOCTOR].user_id)
    assert items[0]["template_code"] == "T_REASON" and items[0]["top_features"]
    assert "patient_name" not in items[0], "the list does not show who the patient is"
    today = items[0]["day"]
    assert len(
        (await client.get(f"/api/v1/alerts?day={today}&status=open", headers=admin.headers)).json()
    ) == len(items)
    assert (await client.get("/api/v1/alerts?day=2000-01-01", headers=admin.headers)).json() == []

    alert_id = items[0]["id"]
    before = await _admin_accesses(admin_engine, clinic, admin.user_id)
    detail = await client.get(f"/api/v1/alerts/{alert_id}", headers=admin.headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["patient_id"] == str(patient) and body["patient_name"]
    assert body["features"]["sigma"] == pytest.approx(0.3)
    assert any(t["is_this_alert"] for t in body["timeline"])
    assert all(len(t["patient_ref"]) == 8 for t in body["timeline"])
    assert await _admin_accesses(admin_engine, clinic, admin.user_id) == before + 1, (
        "seeing the patient behind an alert is itself a recorded access"
    )

    reviewed = await client.post(
        f"/api/v1/alerts/{alert_id}/review",
        json={"outcome": "misuse", "note": "Not covering that day"},
        headers=admin.headers,
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "misuse"
    assert reviewed.json()["reviews"][0]["outcome"] == "misuse"
    async with AsyncSession(admin_engine) as s:
        audit = await s.scalar(
            select(m.AuditEvent).where(
                m.AuditEvent.clinic_id == clinic, m.AuditEvent.kind == "alert_review"
            )
        )
    assert audit is not None and audit.payload["outcome"] == "misuse"


async def _admin_accesses(engine: AsyncEngine, clinic: uuid.UUID, user: uuid.UUID) -> int:
    async with AsyncSession(engine) as s:
        rows = await s.scalars(
            select(m.AccessEvent.id).where(
                m.AccessEvent.clinic_id == clinic, m.AccessEvent.user_id == user
            )
        )
        return len(rows.all())


async def test_only_admins_of_the_clinic_see_alerts(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
    store: service.ModelStore,
) -> None:
    await open_with_reason(client, staff[Role.DOCTOR], patient)
    await score(admin_engine, clinic, store)
    alert_id = (await alerts_of(admin_engine, clinic))[0].id
    for role in (Role.DOCTOR, Role.RECEPTION):
        assert (await client.get("/api/v1/alerts", headers=staff[role].headers)).status_code == 403
    other = await make_actor(admin_engine, await make_clinic(admin_engine), Role.CLINIC_ADMIN)
    assert (await client.get("/api/v1/alerts", headers=other.headers)).json() == []
    assert (
        await client.get(f"/api/v1/alerts/{alert_id}", headers=other.headers)
    ).status_code == 404
    review = await client.post(
        f"/api/v1/alerts/{alert_id}/review", json={"outcome": "benign"}, headers=other.headers
    )
    assert review.status_code == 404


async def test_nobody_reviews_an_alert_about_themselves(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
) -> None:
    admin = staff[Role.CLINIC_ADMIN]
    await client.get(f"/api/v1/patients/{patient}", headers=admin.headers)
    async with AsyncSession(admin_engine) as s, s.begin():
        event = await s.scalar(
            select(m.AccessEvent).where(
                m.AccessEvent.clinic_id == clinic, m.AccessEvent.user_id == admin.user_id
            )
        )
        assert event is not None
        alert = m.Alert(
            clinic_id=clinic,
            access_event_id=event.id,
            day=date.today(),
            score=1.0,
            rank_in_day=1,
            features={"values": {}, "top": [], "scorer": "starter_rules"},
            model_version="x" * 64,
            policy_version="y" * 64,
        )
        s.add(alert)
        await s.flush()
        alert_id = alert.id
    r = await client.post(
        f"/api/v1/alerts/{alert_id}/review", json={"outcome": "benign"}, headers=admin.headers
    )
    assert r.status_code == 403 and r.json()["code"] == "own-alert"
    missing = await client.get(f"/api/v1/alerts/{uuid.uuid4()}", headers=admin.headers)
    assert missing.status_code == 404


async def test_fitted_model_is_versioned_and_used(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
    store: service.ModelStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _ in range(3):
        await open_with_reason(client, staff[Role.DOCTOR], patient)

    async def only_this_clinic() -> list[uuid.UUID]:
        return [clinic]

    monkeypatch.setattr(service, "clinic_ids", only_this_clinic)
    result = await service.fit_models(scorer="iforest", store=store)
    assert set(result["versions"]) == {service.GLOBAL_KEY}, "too few rows for a clinic model"
    loaded = store.load(service.GLOBAL_KEY)
    assert loaded is not None
    bundle, version = loaded
    assert version == result["versions"][service.GLOBAL_KEY] and bundle.threshold is not None

    await score(admin_engine, clinic, store)
    alerts = await alerts_of(admin_engine, clinic)
    assert alerts and {a.model_version for a in alerts} == {version}
    assert alerts[0].features["scorer"] == "iforest"
    assert alerts[0].features["top"][0]["feature"] in bundle.columns
    assert await service.score_all() is not None


async def test_worker_fits_once_a_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = service.ModelStore(tmp_path / "m")
    calls: list[str] = []

    async def fake_fit(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("fit")
        return {}

    async def fake_score(*args: object, **kwargs: object) -> dict[str, int]:
        calls.append("score")
        return {}

    monkeypatch.setattr(service, "model_store", lambda: store)
    monkeypatch.setattr(service, "fit_models", fake_fit)

    async def fake_daily(*args: object, **kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(service, "score_all", fake_score)
    monkeypatch.setattr(daily, "run_due", fake_daily)
    await worker.detect_tick()
    await worker.detect_tick()
    assert calls == ["fit", "score", "score"]
    assert worker.fit_is_due(store, datetime.now(UTC) + worker.FIT_EVERY)


# A review is final (AUDIT_REPORT M3) --------------------------------------------------------------


async def _scored_alert(
    client: AsyncClient,
    engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
    store: service.ModelStore,
) -> uuid.UUID:
    await open_with_reason(client, staff[Role.DOCTOR], patient)
    await score(engine, clinic, store)
    return (await alerts_of(engine, clinic))[0].id


async def _reviews(engine: AsyncEngine, alert_id: uuid.UUID) -> list[m.AlertReview]:
    async with AsyncSession(engine) as s:
        rows = await s.scalars(select(m.AlertReview).where(m.AlertReview.alert_id == alert_id))
        return list(rows.all())


async def test_a_second_review_is_refused(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
    store: service.ModelStore,
) -> None:
    alert_id = await _scored_alert(client, admin_engine, clinic, staff, patient, store)
    admin = staff[Role.CLINIC_ADMIN]
    url = f"/api/v1/alerts/{alert_id}/review"
    first = await client.post(url, json={"outcome": "misuse"}, headers=admin.headers)
    assert first.status_code == 200
    second = await client.post(url, json={"outcome": "benign"}, headers=admin.headers)
    assert second.status_code == 409
    assert second.json()["code"] == "already-reviewed"
    reviews = await _reviews(admin_engine, alert_id)
    assert [r.outcome for r in reviews] == ["misuse"]
    async with AsyncSession(admin_engine) as s:
        alert = await s.get(m.Alert, alert_id)
        audits = (
            await s.scalars(
                select(m.AuditEvent).where(
                    m.AuditEvent.clinic_id == clinic, m.AuditEvent.kind == "alert_review"
                )
            )
        ).all()
    assert alert is not None and alert.status == "misuse"
    assert len(audits) == 1


async def test_two_reviews_at_once_leave_exactly_one(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
    store: service.ModelStore,
) -> None:
    from tests.identity.test_single_use import race

    alert_id = await _scored_alert(client, admin_engine, clinic, staff, patient, store)
    admin = staff[Role.CLINIC_ADMIN]
    results = await race(
        admin_engine,
        "SELECT 1 FROM alerts WHERE id = :id FOR UPDATE",
        lambda: client.post(
            f"/api/v1/alerts/{alert_id}/review", json={"outcome": "unsure"}, headers=admin.headers
        ),
        id=alert_id,
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(await _reviews(admin_engine, alert_id)) == 1
