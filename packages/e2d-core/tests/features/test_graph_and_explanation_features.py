import uuid
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from e2d_core.explain import Explanation
from e2d_core.features import (
    ALL_FEATURES,
    EXPLANATION_FEATURES,
    FORGERY_FEATURES,
    GRAPH_FEATURES,
    ROLE_FEATURES,
    build_features,
    explanation_features,
    explanation_rows,
    flags_from_json,
    graph_features,
)
from e2d_core.features.graph import NO_VISIT_DAYS

T0 = datetime(2026, 3, 2, 4, 0, tzinfo=UTC)
TS = pl.Datetime("us", "UTC")


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def care_frames() -> dict[str, pl.DataFrame]:
    """d1 and d2 are doctors, n1 a nurse who is also patient p2. p1 shares a surname with d1 and
    n1; p3 has no care relation with anyone."""
    return {
        "users": pl.DataFrame(
            {"id": ["d1", "d2", "n1"], "name": ["Dr. Asha Rao", "Vikas Mehta", "Ritu Rao"]}
        ),
        "memberships": pl.DataFrame(
            {"user_id": ["d1", "d2", "n1"], "role": ["doctor", "doctor", "nurse"]}
        ),
        "patients": pl.DataFrame(
            {
                "id": ["p1", "p2", "p3"],
                "user_id": [None, "n1", None],
                "name": ["Kiran Rao", "Ritu Rao", "Uma Shah"],
                "address": ["1, Ward 2", "5, Ward 9", "7, Ward 3"],
            }
        ),
        "appointments": pl.DataFrame(
            {"doctor_user_id": ["d1"], "patient_id": ["p1"], "created_at": [at(0)]}
        ),
        "encounters": pl.DataFrame(
            {"doctor_user_id": ["d2"], "patient_id": ["p1"], "started_at": [at(1)]}
        ),
        "vitals": pl.DataFrame(
            {"recorded_by": ["n1"], "patient_id": ["p2"], "recorded_at": [at(0)]}
        ),
        "care_team_assignments": pl.DataFrame(
            {"user_id": ["d2"], "patient_id": ["p2"], "starts_at": [at(2)]}
        ),
    }


def accesses(rows: list[tuple[str, str, str, float]], role: str = "doctor") -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "id": i,
                "user_id": u,
                "patient_id": p,
                "at": at(h),
                "role": role,
                "action": "view",
                "session_id": "s",
                "device_id": "dev",
            }
            for i, u, p, h in rows
        ],
        schema={
            "id": pl.String,
            "user_id": pl.String,
            "patient_id": pl.String,
            "at": TS,
            "role": pl.String,
            "action": pl.String,
            "session_id": pl.String,
            "device_id": pl.String,
        },
    )


def test_graph_features() -> None:
    events = accesses(
        [
            ("direct", "d1", "p1", 0.5),
            ("via_colleague", "n1", "p1", 3),
            ("stranger", "d1", "p3", 3),
            ("own_record", "n1", "p2", 3),
        ]
    )
    out = graph_features(events, care_frames())
    assert out.columns == ["access_event_id", *GRAPH_FEATURES]
    f = {r.pop("access_event_id"): r for r in out.iter_rows(named=True)}

    assert (f["direct"]["care_direct"], f["direct"]["path_len"]) == (1, 1)
    assert f["direct"]["team_jaccard"] == 0, "d2 joined p1's care only later"
    assert f["direct"]["shares_surname"] == 1 and f["direct"]["shares_address"] == 0
    assert f["direct"]["days_since_last_visit"] == NO_VISIT_DAYS

    v = f["via_colleague"]
    assert (v["care_direct"], v["path_len"]) == (0, 3)
    # n1's colleagues {d2}; p1's team {d1, d2}: intersection 1, union 2.
    assert v["team_jaccard"] == pytest.approx(0.5)
    assert v["days_since_last_visit"] == pytest.approx(2 / 24)
    assert v["is_staff_patient"] == 0 and v["is_self"] == 0

    assert (f["stranger"]["care_direct"], f["stranger"]["path_len"]) == (0, 4)
    assert f["stranger"]["team_jaccard"] == 0

    own = f["own_record"]
    assert (own["is_self"], own["is_staff_patient"], own["shares_address"]) == (1, 1, 1)
    assert own["path_len"] == 1


def test_graph_features_without_care_tables() -> None:
    frames = {k: v for k, v in care_frames().items() if k in ("users", "memberships", "patients")}
    out = graph_features(accesses([("e", "d1", "p1", 1)]), frames)
    assert out.row(0, named=True)["path_len"] == 4


def explanation(code: str | None, strength: float, **flags: bool | None) -> Explanation:
    return Explanation(template_code=code, strength=strength, forgery_flags=dict(flags))


def test_explanation_features() -> None:
    rows = explanation_rows(
        [
            ("a", explanation("T_APPT", 0.9, self_created_recent=False, no_progress=None), False),
            ("b", explanation(None, 0.0), True),
            ("c", explanation("T_REASON", 0.3, self_created_recent=True, no_progress=True), False),
        ]
    )
    out = explanation_features(rows)
    assert out.columns == ["access_event_id", *EXPLANATION_FEATURES, *FORGERY_FEATURES]
    f = {r.pop("access_event_id"): r for r in out.iter_rows(named=True)}
    assert f["a"]["sigma"] == 0.9 and f["a"]["tpl_T_APPT"] == 1 and f["a"]["tpl_none"] == 0
    assert f["a"]["flags_any"] == 0 and f["a"]["flag_no_progress"] == 0, "undecided is not fired"
    assert f["b"]["tpl_none"] == 1 and f["b"]["break_glass"] == 1
    assert (f["c"]["flags_any"], f["c"]["flags_count"]) == (1, 2)
    with pytest.raises(ValueError, match="strength"):
        explanation_features(rows.drop("strength"))


def test_flags_from_json_keeps_only_booleans() -> None:
    flags = flags_from_json({"self_created_recent": True, "self_creation_high": 2.5})
    assert flags["flag_self_created_recent"] is True
    assert flags["flag_self_creation_high"] is None
    assert flags_from_json(None)["flag_no_progress"] is None


def test_build_features_joins_every_group() -> None:
    frames = care_frames() | {
        "shifts": pl.DataFrame(
            {"user_id": ["d1"], "starts_at": [at(0)], "ends_at": [at(8)], "status": ["scheduled"]}
        ),
        "sessions": pl.DataFrame({"id": ["s"], "created_at": [at(0)]}),
        "devices": pl.DataFrame({"id": ["dev"], "first_seen": [at(-100)]}),
    }
    events = accesses([("x", "d1", "p1", 1), ("y", "d1", "p3", 2)])
    rows = explanation_rows(
        [("y", explanation(None, 0.0), False), ("x", explanation("T_APPT", 1.0), False)]
    )
    out = build_features(events, frames, rows, role_onehot=True)
    assert out["access_event_id"].to_list() == ["x", "y"]
    assert out.columns == ["access_event_id", *ALL_FEATURES, *ROLE_FEATURES]
    assert out["sigma"].to_list() == [1.0, 0.0]
    assert out["role_doctor"].to_list() == [1, 1]
    assert out.null_count().sum_horizontal().item() == 0
    assert str(uuid.uuid4())  # ids may be any strings
