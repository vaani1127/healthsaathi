from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from saathibench.attacks import ATTACK_NAMES
from saathibench.audit import FEATURES, audit
from saathibench.config import AttackConfig, load_config, parse_attacks
from saathibench.labels import attack_labels, campaigns
from saathibench.run import simulate
from saathibench.splits import HELD_OUT_TYPES, assign, make_splits

SMALL = Path(__file__).resolve().parents[1] / "configs" / "small.yaml"


def frame(run: Path, table: str) -> pl.DataFrame:
    return pl.concat([pl.read_parquet(f) for f in sorted((run / "tables" / table).glob("*"))])


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Many campaigns over seven weeks, so every attack type gets a chance to happen."""
    cfg = load_config(SMALL)
    cfg = replace(
        cfg,
        start_date=date(2026, 1, 5),
        days=50,
        attacks=AttackConfig(campaigns_per_clinic_month=14, first_day=3),
    )
    out = tmp_path_factory.mktemp("attacks") / "run"
    simulate(cfg, out, workers=3)
    return out


def test_every_attack_type_happens(run: Path) -> None:
    found = campaigns(run)
    assert set(found["attack_type"]) == set(ATTACK_NAMES)
    assert found["mimicry"].is_between(0, 1).all()
    assert (found["targets"] > 0).all()
    assert found["attack_name"].to_list() == [ATTACK_NAMES[t] for t in found["attack_type"]]


def test_every_access_has_exactly_one_label(run: Path) -> None:
    events = frame(run, "access_events")
    labels = attack_labels(run)
    assert labels.height == events.height
    assert set(labels["access_event_id"]) == set(events["id"])
    attacks = labels.filter(pl.col("is_attack"))
    benign = labels.filter(~pl.col("is_attack"))
    assert attacks["attack_type"].null_count() == 0 and attacks["campaign_id"].null_count() == 0
    assert benign["attack_type"].null_count() == benign.height
    assert set(attacks["campaign_id"]) == set(campaigns(run)["campaign_id"])
    # Attacks are a small minority of accesses.
    assert 0 < attacks.height < 0.05 * labels.height


def test_attack_rows_look_like_any_other_access(run: Path) -> None:
    events = frame(run, "access_events")
    assert "is_attack" not in events.columns and "attack_type" not in events.columns
    labelled = events.join(
        attack_labels(run).select(pl.col("access_event_id").alias("id"), "attack_type"), on="id"
    )
    actors = campaigns(run).select("campaign_id", "actor_user_id")
    attack_rows = labelled.filter(pl.col("attack_type").is_not_null())
    assert attack_rows["user_id"].is_in(actors["actor_user_id"].implode()).all()


def test_forged_and_side_evidence_is_in_the_tables(run: Path) -> None:
    labels = attack_labels(run)
    events = frame(run, "access_events").join(
        labels.select(pl.col("access_event_id").alias("id"), "attack_type"), on="id"
    )
    glass_abuse = events.filter(pl.col("attack_type") == 8)
    assert glass_abuse["break_glass_id"].null_count() == 0
    assert set(glass_abuse["break_glass_id"]) <= set(frame(run, "break_glass_events")["id"])
    edits = frame(run, "clinical_notes").filter(pl.col("version") == 2)
    assert edits.height > 0 and edits["parent_id"].null_count() == 0
    shared = events.filter(pl.col("attack_type") == 7)
    devices = frame(run, "devices").select(pl.col("id").alias("device_id"), "first_seen")
    first_use = shared.group_by("device_id").agg(pl.col("at").min()).join(devices, on="device_id")
    # The borrowed account is used from a device nobody had used before.
    assert ((first_use["at"] - first_use["first_seen"]).dt.total_minutes() < 60).all()


def test_attack_config_parsing() -> None:
    assert parse_attacks({}).campaigns_per_clinic_month == 0
    fixed = parse_attacks({"mimicry": [0, 0.5, 1], "types": [6, 10]})
    assert fixed.mimicry == (0.0, 0.5, 1.0) and fixed.types == (6, 10)
    with pytest.raises(ValueError, match="between 1 and 10"):
        parse_attacks({"types": [11]})
    with pytest.raises(ValueError, match="mimicry"):
        parse_attacks({"mimicry": [1.5]})
    assert load_config(SMALL).attacks.campaigns_per_clinic_month > 0


def test_splits(run: Path) -> None:
    definitions = make_splits(run, seed=3)
    assert (run / "splits.json").exists()
    events = frame(run, "access_events")
    labels = attack_labels(run)

    temporal = assign(events, labels, "temporal", definitions)
    assert set(temporal.unique()) == {"train", "test"}

    attack = assign(events, labels, "attack", definitions)
    with_types = labels.select(pl.col("access_event_id").alias("id"), "attack_type")
    joined = events.select("id").with_columns(attack.alias("split")).join(with_types, on="id")
    held = joined.filter(pl.col("attack_type").is_in(HELD_OUT_TYPES))
    assert held.filter(pl.col("split") == "train").is_empty()

    clinic = assign(events, labels, "clinic", definitions)
    per_clinic = events.select("clinic_id").with_columns(clinic.alias("split")).unique()
    assert per_clinic.group_by("clinic_id").len()["len"].max() == 1, "a clinic is on one side"
    assert set(per_clinic["split"]) == {"train", "test"}

    user = assign(events, labels, "user", definitions)
    per_user = events.select("user_id").with_columns(user.alias("split")).unique()
    assert per_user.group_by("user_id").len()["len"].max() == 1
    assert make_splits(run, seed=3) == definitions, "same seed, same splits"
    with pytest.raises(ValueError, match="unknown split"):
        assign(events, labels, "random", definitions)


def test_audit_report(run: Path) -> None:
    report = audit(run, max_benign=50_000)
    assert set(report["single_feature_auc"]) == set(FEATURES)
    assert all(0.5 <= v <= 1 for v in report["single_feature_auc"].values())
    assert 0.5 <= report["depth2_tree_auc"] <= 1
    assert report["passed"] == (not report["failures"])
    assert set(report["by_attack_type_info"]) == {str(t) for t in ATTACK_NAMES}
