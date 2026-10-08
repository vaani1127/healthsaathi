from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from e2d_core.features import BEHAVIOUR_FEATURES, behaviour_features

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def events(rows: list[tuple[str, str, str, str, float, str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "id": i,
                "user_id": u,
                "patient_id": p,
                "action": a,
                "at": at(m),
                "session_id": s,
                "device_id": d,
            }
            for i, u, p, a, m, s, d in rows
        ],
        schema={
            "id": pl.String,
            "user_id": pl.String,
            "patient_id": pl.String,
            "action": pl.String,
            "at": pl.Datetime("us", "UTC"),
            "session_id": pl.String,
            "device_id": pl.String,
        },
    )


SHIFTS = pl.DataFrame(
    {
        "user_id": ["u1", "u1", "u2", "u2"],
        "starts_at": [at(-30), at(100), at(-30), at(0)],
        "ends_at": [at(60), at(200), at(500), at(10)],
        "status": ["scheduled", "scheduled", "cancelled", "scheduled"],
    }
)
SESSIONS = pl.DataFrame({"id": ["s1", "s2"], "created_at": [at(-20), at(5)]})
DEVICES = pl.DataFrame({"id": ["d1", "d2"], "first_seen": [at(-60 * 24 * 30), at(-60)]})


def by_id(frame: pl.DataFrame) -> dict[str, dict[str, float]]:
    return {r.pop("access_event_id"): r for r in frame.iter_rows(named=True)}


def test_rolling_counts_windows_and_exports() -> None:
    ev = events(
        [
            ("e1", "u1", "p1", "view", 0, "s1", "d1"),
            ("e2", "u1", "p2", "view", 30, "s1", "d1"),
            ("e3", "u1", "p1", "export", 50, "s1", "d1"),
            ("e4", "u1", "p3", "print", 120, "s1", "d1"),
            ("e5", "u2", "p9", "view", 30, "s2", "d2"),
            ("e6", "u1", "p4", "view", 60 * 24 * 3, "s1", "d1"),
        ]
    )
    out = behaviour_features(ev, SHIFTS, SESSIONS, DEVICES)
    assert out.columns == ["access_event_id", *BEHAVIOUR_FEATURES]
    assert out["access_event_id"].to_list() == ["e1", "e2", "e3", "e4", "e5", "e6"]
    f = by_id(out)
    assert (f["e1"]["accesses_1h"], f["e1"]["patients_1h"], f["e1"]["exports_1h"]) == (1, 1, 0)
    assert (f["e3"]["accesses_1h"], f["e3"]["patients_1h"], f["e3"]["exports_1h"]) == (3, 2, 1)
    # e4's one-hour window is (60, 120] minutes, so only e4 itself is in it.
    assert (f["e4"]["accesses_1h"], f["e4"]["exports_1h"]) == (1, 1)
    assert (f["e4"]["accesses_24h"], f["e4"]["patients_24h"]) == (4, 3)
    assert f["e5"]["accesses_24h"] == 1, "other users do not count"
    assert (f["e6"]["accesses_24h"], f["e6"]["accesses_7d"], f["e6"]["patients_7d"]) == (1, 5, 4)


def test_shift_device_and_session_context() -> None:
    ev = events(
        [
            ("in", "u1", "p1", "view", 10, "s1", "d1"),
            ("gap", "u1", "p1", "view", 80, "s1", "d1"),
            ("evening", "u1", "p1", "view", 150, "s1", "d1"),
            ("cancelled", "u2", "p1", "view", 100, "s2", "d2"),
            ("short", "u2", "p1", "view", 5, "s2", "d2"),
            ("nobody", "u3", "p1", "view", 0, "sx", "dx"),
        ]
    )
    f = by_id(behaviour_features(ev, SHIFTS, SESSIONS, DEVICES))
    assert [f[k]["off_shift"] for k in ("in", "gap", "evening")] == [0, 1, 0]
    assert f["cancelled"]["off_shift"] == 1, "a cancelled shift does not count"
    assert f["short"]["off_shift"] == 0
    assert f["nobody"]["off_shift"] == 1
    assert f["in"]["new_device"] == 0
    assert f["short"]["new_device"] == 1, "d2 was first seen an hour earlier"
    assert f["nobody"]["new_device"] == 1, "an unknown device counts as new"
    assert f["in"]["session_age_min"] == pytest.approx(30)
    assert f["short"]["session_age_min"] == 0, "never negative"
    assert f["nobody"]["session_age_min"] == 0


def test_overlapping_shifts_cover_their_union() -> None:
    shifts = pl.DataFrame(
        {
            "user_id": ["u1", "u1"],
            "starts_at": [at(0), at(10)],
            "ends_at": [at(300), at(20)],
            "status": ["scheduled", "scheduled"],
        }
    )
    ev = events([("e", "u1", "p", "view", 100, "s1", "d1")])
    assert behaviour_features(ev, shifts, SESSIONS, DEVICES)["off_shift"].to_list() == [0]


def test_missing_columns_are_reported() -> None:
    with pytest.raises(ValueError, match="device_id"):
        behaviour_features(events([]).drop("device_id"), SHIFTS, SESSIONS, DEVICES)
