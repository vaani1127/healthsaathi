"""Graph features of an access (SPEC 5.4), from the clinic's care graph at the time of access.

The care graph links a user u and a patient p when u has a care relation with p: an appointment
or encounter as the doctor, a referral to u, a care-team assignment, a lab order or result, or
vitals recorded by u. An edge counts from the time its evidence was created. For access (u, p, t):

- care_direct: u has an edge to p before t.
- path_len: shortest path from u to p, capped at 4. 1 is a direct edge; 3 means a colleague v
  with an edge to p shares at least one patient with u; 4 means neither.
- team_jaccard: Jaccard index of u's colleagues (users sharing a patient with u) and p's care team.
- is_staff_patient, is_self: p is a staff member of the clinic, or u themselves.
- shares_surname, shares_address: p has u's surname, or u's own address (relative or neighbour
  proxies; u's address comes from u's own patient record, when there is one).
- days_since_last_visit: days since p's last encounter before t (365 when none).

Only rows created at or before t are used, so the features can be computed when the access
happens.
"""

from collections.abc import Mapping

import polars as pl

EDGE_SOURCES = (
    ("appointments", "doctor_user_id", "created_at"),
    ("encounters", "doctor_user_id", "started_at"),
    ("referrals", "to_user_id", "created_at"),
    ("care_team_assignments", "user_id", "starts_at"),
    ("lab_orders", "ordered_by", "created_at"),
    ("lab_results", "resulted_by", "resulted_at"),
    ("vitals", "recorded_by", "recorded_at"),
)
NO_VISIT_DAYS = 365.0
FEATURES = (
    "care_direct",
    "path_len",
    "team_jaccard",
    "is_staff_patient",
    "is_self",
    "shares_surname",
    "shares_address",
    "days_since_last_visit",
)


def care_edges(frames: Mapping[str, pl.DataFrame], ts: pl.DataType) -> pl.DataFrame:
    """user_id, patient_id, since: the first time each care relation existed."""
    parts = []
    for table, user_column, time_column in EDGE_SOURCES:
        frame = frames.get(table)
        if frame is None or frame.is_empty():
            continue
        parts.append(
            frame.select(
                pl.col(user_column).cast(pl.String).alias("user_id"),
                pl.col("patient_id").cast(pl.String),
                pl.col(time_column).cast(ts).alias("since"),
            )
        )
    schema: dict[str, pl.DataType] = {
        "user_id": pl.String(),
        "patient_id": pl.String(),
        "since": ts,
    }
    if not parts:
        return pl.DataFrame(schema=schema)
    return (
        pl.concat(parts)
        .drop_nulls()
        .group_by("user_id", "patient_id")
        .agg(pl.col("since").min())
        .sort("user_id", "patient_id")
    )


def _surname(column: str) -> pl.Expr:
    return pl.col(column).str.strip_chars().str.split(" ").list.last().str.to_lowercase()


def graph_features(events: pl.DataFrame, frames: Mapping[str, pl.DataFrame]) -> pl.DataFrame:
    """access_event_id plus FEATURES, in the order of `events` (id, user_id, patient_id, at)."""
    ts = events.schema["at"]
    base = events.select(
        pl.col("id").cast(pl.String),
        pl.col("user_id").cast(pl.String),
        pl.col("patient_id").cast(pl.String),
        "at",
    )
    edges = care_edges(frames, ts)

    direct = base.join(edges, on=["user_id", "patient_id"], how="left").select(
        "id", (pl.col("since").is_not_null() & (pl.col("since") <= pl.col("at"))).alias("direct")
    )

    # Colleagues: pairs of users who share a patient, from the moment both had an edge to them.
    pairs = (
        edges.join(edges, on="patient_id", suffix="_v")
        .filter(pl.col("user_id") != pl.col("user_id_v"))
        .select(
            "user_id",
            pl.col("user_id_v").alias("colleague"),
            pl.max_horizontal("since", "since_v").alias("shared"),
        )
        .group_by("user_id", "colleague")
        .agg(pl.col("shared").min())
    )
    team = (
        base.join(edges.rename({"user_id": "member", "since": "member_since"}), on="patient_id")
        .filter((pl.col("member") != pl.col("user_id")) & (pl.col("member_since") <= pl.col("at")))
        .join(pairs.rename({"colleague": "member"}), on=["user_id", "member"], how="left")
        .group_by("id")
        .agg(
            pl.len().alias("team_size"),
            (pl.col("shared").is_not_null() & (pl.col("shared") <= pl.col("at")))
            .sum()
            .alias("in_both"),
        )
    )
    # How many colleagues u had at time t: a running count over u's colleague start times.
    colleague_counts = (
        pairs.sort("user_id", "shared")
        .with_columns(pl.int_range(1, pl.len() + 1).over("user_id").alias("colleagues"))
        .select("user_id", "shared", "colleagues")
        .sort("shared")
    )
    colleagues = (
        base.select("id", "user_id", "at")
        .sort("at")
        .join_asof(
            colleague_counts,
            left_on="at",
            right_on="shared",
            by="user_id",
            strategy="backward",
            check_sortedness=False,  # both sides are sorted on their time column just above
        )
        .select("id", pl.col("colleagues").fill_null(0))
    )

    patients = frames["patients"].select(
        pl.col("id").cast(pl.String).alias("patient_id"),
        pl.col("user_id").cast(pl.String).alias("patient_user_id"),
        _surname("name").alias("patient_surname"),
        pl.col("address").alias("patient_address"),
    )
    staff_users = (
        frames["memberships"]
        .filter(pl.col("role") != "patient")
        .select(pl.col("user_id").cast(pl.String).unique())
        .to_series()
    )
    users = frames["users"].select(
        pl.col("id").cast(pl.String).alias("user_id"), _surname("name").alias("user_surname")
    )
    own_address = (
        frames["patients"]
        .filter(pl.col("user_id").is_not_null())
        .select(pl.col("user_id").cast(pl.String), pl.col("address").alias("user_address"))
        .unique("user_id", keep="first")
    )
    identity = (
        base.select("id", "user_id", "patient_id")
        .join(patients, on="patient_id", how="left", maintain_order="left")
        .join(users, on="user_id", how="left", maintain_order="left")
        .join(own_address, on="user_id", how="left", maintain_order="left")
        .select(
            "id",
            pl.col("patient_user_id").is_in(staff_users.implode()).fill_null(False).alias("staff"),
            (pl.col("patient_user_id") == pl.col("user_id")).fill_null(False).alias("self"),
            (pl.col("patient_surname") == pl.col("user_surname")).fill_null(False).alias("surname"),
            (pl.col("patient_address") == pl.col("user_address")).fill_null(False).alias("address"),
        )
    )

    visits = frames.get("encounters")
    if visits is None or visits.is_empty():
        last_visit = base.select("id", pl.lit(None, dtype=ts).alias("last_visit"))
    else:
        started = (
            visits.select(
                pl.col("patient_id").cast(pl.String), pl.col("started_at").cast(ts).alias("last")
            )
            .drop_nulls()
            .sort("last")
        )
        last_visit = (
            base.select("id", "patient_id", "at")
            .sort("at")
            .join_asof(
                started,
                left_on="at",
                right_on="last",
                by="patient_id",
                strategy="backward",
                check_sortedness=False,
            )
            .select("id", pl.col("last").alias("last_visit"))
        )

    joined = (
        base.select("id", "at")
        .join(direct, on="id", how="left", maintain_order="left")
        .join(team, on="id", how="left", maintain_order="left")
        .join(colleagues, on="id", how="left", maintain_order="left")
        .join(identity, on="id", how="left", maintain_order="left")
        .join(last_visit, on="id", how="left", maintain_order="left")
        .with_columns(pl.col("team_size", "in_both", "colleagues").fill_null(0))
    )
    union = pl.col("colleagues") + pl.col("team_size") - pl.col("in_both")
    return joined.select(
        pl.col("id").alias("access_event_id"),
        pl.col("direct").cast(pl.Int8).alias("care_direct"),
        pl.when(pl.col("direct"))
        .then(1)
        .when(pl.col("in_both") > 0)
        .then(3)
        .otherwise(4)
        .cast(pl.Int8)
        .alias("path_len"),
        pl.when(union > 0).then(pl.col("in_both") / union).otherwise(0.0).alias("team_jaccard"),
        pl.col("staff").cast(pl.Int8).alias("is_staff_patient"),
        pl.col("self").cast(pl.Int8).alias("is_self"),
        pl.col("surname").cast(pl.Int8).alias("shares_surname"),
        pl.col("address").cast(pl.Int8).alias("shares_address"),
        ((pl.col("at") - pl.col("last_visit")).dt.total_seconds() / 86400)
        .clip(upper_bound=NO_VISIT_DAYS)
        .fill_null(NO_VISIT_DAYS)
        .alias("days_since_last_visit"),
    )
