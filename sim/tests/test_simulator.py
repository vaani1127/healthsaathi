import enum
import json
import random
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.access.policy import get_policy
from app.db import enums
from e2d_core.repo.memory import COLUMNS
from saathibench.config import RunConfig, load_config
from saathibench.coverage import coverage
from saathibench.ids import Ids
from saathibench.policy import load_policy
from saathibench.run import simulate
from saathibench.schema import LABEL_TABLES, TABLES

SIM = Path(__file__).resolve().parents[1]
SMALL = SIM / "configs" / "small.yaml"


def small(days: int = 10, start: date | None = None) -> RunConfig:
    cfg = load_config(SMALL)
    # Starting near a month end makes the month-end hard negative appear in a short run.
    return replace(cfg, days=days, start_date=start or date(2026, 1, 26))


def frame(run: Path, table: str, folder: str = "tables") -> pl.DataFrame:
    files = sorted((run / folder / table).glob("*.parquet"))
    return pl.concat([pl.read_parquet(f) for f in files]) if files else pl.DataFrame()


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("sim") / "run"
    simulate(small(), out, workers=1)
    return out


def test_configs_load() -> None:
    v1 = load_config(SIM / "configs" / "v1.yaml")
    assert len(v1.clinics) == 30 and v1.days == 180
    assert [c.index for c in v1.clinics] == list(range(30))
    assert {c.profile.name for c in v1.clinics} == {"solo_gp", "polyclinic", "nursing_home"}


def test_policy_is_the_products_policy() -> None:
    ours, product = load_policy(), get_policy()
    assert ours.sha256 == product.sha256
    assert ours.rules == {k: v.value for k, v in product.rules.items()}
    assert ours.allows("nurse", "notes", "view") is False
    assert ours.allows("nurse", "notes", "view", break_glass=True) is True
    assert ours.allows("reception", "notes", "view") is False


def test_ids_are_uuid7_and_reproducible() -> None:
    at = datetime(2026, 1, 5, 9, tzinfo=UTC)
    a, b = Ids(random.Random(1)), Ids(random.Random(1))
    first = a.new(at)
    assert first == b.new(at)
    parsed = uuid.UUID(first)
    assert parsed.version == 7 and parsed.variant == uuid.RFC_4122
    assert int(parsed.hex[:12], 16) == int(at.timestamp() * 1000)


def test_tables_have_the_product_shapes(run: Path) -> None:
    for table, schema in TABLES.items():
        data = frame(run, table)
        if data.is_empty():
            continue
        assert dict(data.schema) == schema, table
        if table in COLUMNS:
            assert set(COLUMNS[table]) <= set(schema), table
    # Labels live only in the labels folder.
    assert not (run / "tables" / "benign_scenarios").exists()
    assert set(LABEL_TABLES) == {p.name for p in (run / "labels").iterdir()}
    assert "scenario" not in frame(run, "access_events").columns


def _values(e: type[enum.Enum]) -> set[str]:
    return {m.value for m in e}


def test_values_are_valid_product_values(run: Path) -> None:
    checks = {
        ("access_events", "role"): _values(enums.Role),
        ("access_events", "resource_type"): _values(enums.ResourceType),
        ("access_events", "action"): _values(enums.AccessAction),
        ("access_events", "decision"): _values(enums.AccessDecision),
        ("appointments", "kind"): _values(enums.AppointmentKind),
        ("appointments", "status"): _values(enums.AppointmentStatus),
        ("appointments", "source"): _values(enums.AppointmentSource),
        ("queue_tokens", "status"): _values(enums.QueueStatus),
        ("encounters", "status"): _values(enums.EncounterStatus),
        ("referrals", "status"): _values(enums.ReferralStatus),
        ("lab_orders", "status"): _values(enums.LabOrderStatus),
        ("invoices", "status"): _values(enums.InvoiceStatus),
        ("payments", "method"): _values(enums.PaymentMethod),
        ("shifts", "status"): _values(enums.ShiftStatus),
        ("shifts", "role"): _values(enums.Role),
        ("memberships", "role"): _values(enums.Role),
        ("care_team_assignments", "status"): _values(enums.RecordStatus),
        ("schedules", "status"): _values(enums.RecordStatus),
        ("consents", "channel"): _values(enums.ConsentChannel),
        ("patients", "sex"): _values(enums.Sex),
        ("allergies", "severity"): _values(enums.AllergySeverity),
        ("conditions", "status"): _values(enums.ConditionStatus),
    }
    for (table, column), allowed in checks.items():
        found = set(frame(run, table)[column].drop_nulls().unique().to_list())
        assert found and found <= allowed, (table, column, found - allowed)


def test_decisions_follow_the_policy(run: Path) -> None:
    policy = load_policy()
    events = frame(run, "access_events")
    assert events["policy_version"].unique().to_list() == [policy.sha256]
    for row in (
        events.select("role", "resource_type", "action", "decision", "break_glass_id")
        .unique()
        .iter_rows(named=True)
    ):
        allowed = policy.allows(
            row["role"], row["resource_type"], row["action"], row["break_glass_id"] is not None
        )
        assert (row["decision"] == "allow") == allowed, row


def test_references_point_at_existing_rows(run: Path) -> None:
    events = frame(run, "access_events")
    users = set(frame(run, "users")["id"])
    assert set(events["user_id"]) <= users
    assert set(events["patient_id"]) <= set(frame(run, "patients")["id"])
    assert set(events["clinic_id"]) <= set(frame(run, "clinics")["id"])
    assert set(events["session_id"]) <= set(frame(run, "sessions")["id"])
    assert set(events["device_id"]) <= set(frame(run, "devices")["id"])
    glass = set(events["break_glass_id"].drop_nulls())
    assert glass <= set(frame(run, "break_glass_events")["id"])
    appointments = set(frame(run, "appointments")["id"])
    assert set(frame(run, "queue_tokens")["appointment_id"]) <= appointments
    assert set(frame(run, "encounters")["appointment_id"]) <= appointments
    assert set(frame(run, "lab_results")["lab_order_id"]) <= set(frame(run, "lab_orders")["id"])
    assert set(frame(run, "memberships")["user_id"]) <= users
    staff_events = events.filter(pl.col("role") != "patient")
    members = frame(run, "memberships").select("clinic_id", "user_id", "role")
    joined = staff_events.join(members, on=["clinic_id", "user_id", "role"], how="anti")
    assert joined.is_empty(), "every staff access uses a role the user holds in that clinic"
    labels = frame(run, "benign_scenarios", "labels")
    assert set(labels["access_event_id"]) <= set(events["id"])


def test_synthetic_identities_only(run: Path) -> None:
    emails = frame(run, "users")["email"].to_list()
    assert all(e.endswith("@saathibench.test") for e in emails)
    assert frame(run, "patients")["abha_number"].null_count() == frame(run, "patients").height


def test_benign_hard_negatives_appear(run: Path) -> None:
    scenarios = set(frame(run, "benign_scenarios", "labels")["scenario"])
    expected = {"cover_doctor", "nurse_lab_followup", "pharmacy_check", "month_end_billing"}
    assert expected <= scenarios


def test_same_seed_same_output(run: Path, tmp_path: Path) -> None:
    again = tmp_path / "again"
    simulate(small(), again, workers=2)
    for table in ("access_events", "appointments", "patients"):
        assert frame(run, table).equals(frame(again, table)), table
    manifest = json.loads((again / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["seed"] == 7 and manifest["days"] == 10


def test_coverage_report(run: Path) -> None:
    report = coverage(run, per_role=40)
    roles = {"doctor", "nurse", "reception", "lab_tech", "patient", "clinic_admin"}
    assert set(report["by_role"]) == roles
    for row in report["by_role"].values():
        assert row["n"] > 0 and 0.0 <= row["coverage"] <= 1.0
    assert report["by_scenario"]
