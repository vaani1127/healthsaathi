"""Late-flag mismatches on the real service path (ADR 0008).

A normal OPD day is recorded through the API, so every access goes through record_and_explain at
the moment it happens: reception registers the patient, lists patients and books a walk-in (which
issues a queue token), the nurse opens the queue, takes the token, records vitals and passes it on,
the doctor opens the queue and the chart and closes the token (the appointment goes to in
consultation and completed). The nurse works inside a shift. Then the day is finalised and every
staff access is re-explained as of 24 hours later.

The late flags of each access are computed for the evidence its stored explanation names
(status decided). They do not apply when that evidence is a queue token, a registration, a typed
reason or break-glass (not_applicable), and stay undecided only when the named record cannot be
found. The test requires undecided late flags on at most 1% of the accesses where the flags
apply, gated or not (an access whose late flags are lost can no longer enter the gate through
them), prints the share of all staff accesses too, and checks that the daily job's counts agree
with an independent count. The measurement is printed (run with -s).
"""

import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import Role
from app.detect import daily, service
from app.detect.days import ready_after
from e2d_core.detect import gate
from tests.helpers import Actor, make_actor, make_clinic

IST = ZoneInfo("Asia/Kolkata")
VISITS = 12
# Undecided late flags allowed, as a share of the accesses where the late flags apply.
MAX_UNDECIDED = 0.01


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


@pytest.fixture
async def staff(admin_engine: AsyncEngine, clinic: uuid.UUID) -> dict[Role, Actor]:
    return {
        role: await make_actor(admin_engine, clinic, role)
        for role in (Role.RECEPTION, Role.NURSE, Role.DOCTOR, Role.CLINIC_ADMIN)
    }


@pytest.fixture(autouse=True)
def only_this_clinic(monkeypatch: pytest.MonkeyPatch, clinic: uuid.UUID) -> None:
    async def ids() -> list[uuid.UUID]:
        return [clinic]

    monkeypatch.setattr(daily, "clinic_ids", ids)


async def call(client: AsyncClient, actor: Actor, method: str, path: str, **kw: Any) -> Any:
    r = await client.request(method, f"/api/v1{path}", headers=actor.headers, **kw)
    assert r.status_code < 300, (method, path, r.status_code, r.text)
    return r.json()


async def opd_day(client: AsyncClient, staff: dict[Role, Actor], assign_nurse: bool) -> None:
    reception, nurse, doctor = staff[Role.RECEPTION], staff[Role.NURSE], staff[Role.DOCTOR]
    now = datetime.now(IST)
    await call(
        client,
        staff[Role.CLINIC_ADMIN],
        "POST",
        "/shifts",
        json={
            "user_id": str(nurse.user_id),
            "role": "nurse",
            "starts_at": (now - timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(hours=8)).isoformat(),
        },
    )
    for i in range(VISITS):
        patient = await call(
            client, reception, "POST", "/patients", json={"name": f"Lifecycle {i}", "sex": "female"}
        )
        pid = patient["id"]
        await call(client, reception, "GET", "/patients", params={"limit": 25})
        walkin = await call(
            client,
            reception,
            "POST",
            "/walkins",
            json={"patient_id": pid, "doctor_user_id": str(doctor.user_id)},
        )
        token = walkin["token"]["id"]
        await call(client, nurse, "GET", "/queue")
        await call(client, nurse, "PATCH", f"/queue/tokens/{token}", json={"status": "with_nurse"})
        if assign_nurse:
            await call(
                client,
                reception,
                "PATCH",
                f"/queue/tokens/{token}",
                json={"assigned_nurse_user_id": str(nurse.user_id)},
            )
        await call(
            client,
            nurse,
            "POST",
            f"/patients/{pid}/vitals",
            json={"bp_sys": 120, "bp_dia": 80, "pulse": 76, "temp_c": 37.0, "spo2": 98},
        )
        await call(client, nurse, "PATCH", f"/queue/tokens/{token}", json={"status": "with_doctor"})
        await call(client, doctor, "GET", "/queue")
        await call(client, doctor, "GET", f"/patients/{pid}/chart")
        await call(client, doctor, "PATCH", f"/queue/tokens/{token}", json={"status": "done"})


def template_of(row: dict[str, Any]) -> str:
    for name, value in row.items():
        if name.startswith("tpl_") and value:
            return name[4:]
    return "none"


async def measure(engine: AsyncEngine, clinic: uuid.UUID) -> dict[str, Any]:
    """Independent count over the day's staff accesses: stored template, gated or not, and
    whether the late flags were decided."""
    async with AsyncSession(engine) as s:
        latest = await s.scalar(
            select(m.AccessEvent.at)
            .where(m.AccessEvent.clinic_id == clinic)
            .order_by(m.AccessEvent.at.desc())
        )
    assert latest is not None
    day = latest.astimezone(IST).date()
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        rows = (await daily.day_features(db, clinic, day, "Asia/Kolkata")).rows
    flag_cols = [c for c in rows.columns if c.startswith("flag_")]
    marked = rows.with_columns(
        pl.Series("template", [template_of(r) for r in rows.iter_rows(named=True)]),
        gate(rows).alias("gated"),
        (pl.col("late_status") == "undecided").alias("undecided"),
        pl.col("late_status").is_in(["decided", "undecided"]).alias("applies"),
    )
    by_template = (
        marked.group_by("template")
        .agg(
            pl.len().alias("accesses"),
            pl.col("gated").sum().alias("gated"),
            pl.col("applies").sum().alias("flags_apply"),
            pl.col("undecided").sum().alias("undecided"),
            (pl.col("undecided") & pl.col("gated")).sum().alias("gated_undecided"),
        )
        .sort("template")
    )
    why_gated = Counter(
        (r["template"], ",".join(c[5:] for c in flag_cols if r[c]) or "low strength")
        for r in marked.filter(pl.col("gated")).iter_rows(named=True)
    )
    return {
        "day": day,
        "by_template": by_template,
        "accesses": rows.height,
        "gated": int(marked["gated"].sum()),
        "undecided": int(marked["undecided"].sum()),
        "applies": int(marked["applies"].sum()),
        "gated_undecided": int((marked["undecided"] & marked["gated"]).sum()),
        "why_gated": why_gated,
        "statuses": Counter(marked["late_status"].to_list()),
    }


def report(label: str, found: dict[str, Any]) -> None:
    accesses, gated, applies = found["accesses"], found["gated"], found["applies"]
    undecided = found["undecided"]
    rate = 100 * undecided / applies if applies else 0.0
    all_rate = 100 * undecided / accesses if accesses else 0.0
    print(f"\n== {label}: {accesses} staff accesses, {gated} gated, late flags apply to {applies}")
    print(f"late flags undecided: {undecided} of {applies} where they apply = {rate:.2f}%")
    print(
        f"  (all staff accesses: {undecided} of {accesses} = {all_rate:.2f}%;"
        f" gated: {found['gated_undecided']} of {gated})"
    )
    with pl.Config(tbl_rows=50, tbl_hide_dataframe_shape=True, tbl_formatting="ASCII_MARKDOWN"):
        print(found["by_template"])
    print("late flag status:", dict(found["statuses"]))
    print("gated by:", dict(found["why_gated"]))


@pytest.mark.parametrize("assign_nurse", [False, True], ids=["normal", "nurse-assigned"])
async def test_late_flags_are_decided_for_a_day_recorded_through_the_api(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    tmp_path: Any,
    assign_nurse: bool,
) -> None:
    await opd_day(client, staff, assign_nurse)
    found = await measure(admin_engine, clinic)
    report("nurse assigned to the token" if assign_nurse else "normal lifecycle", found)
    assert found["applies"] > 0
    assert found["undecided"] <= MAX_UNDECIDED * found["applies"]
    day: date = found["day"]
    result = await daily.finalise_day(
        clinic,
        day,
        now=ready_after(day, IST) + timedelta(minutes=1),
        run=daily.new_run(service.ModelStore(tmp_path / "models")),
    )
    assert result is not None
    assert result.payload["staff_accesses"] == found["accesses"]
    assert result.payload["gated"] == found["gated"]
    assert result.payload["late_flags_undecided"] == found["undecided"]
    assert result.payload["late_flags_undecided_gated"] == found["gated_undecided"]
