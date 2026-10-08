"""The four evaluation splits (SPEC 7).

- temporal: the first two thirds of the days train, the rest test (days 1-120 and 121-180 in v1).
- clinic: two thirds of the clinics of each profile train, the rest test (20 and 10 in v1).
- attack: as temporal, but attacks of the held-out types (6 and 10) are left out of training.
- user: two thirds of the users train, the rest test, over all days.

`make_splits` writes the definitions to `<run>/splits.json`; `assign` turns a definition into a
train or test label per access event (null means the event is not used).
"""

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

SPLITS = ("temporal", "clinic", "attack", "user")
HELD_OUT_TYPES = (6, 10)
TRAIN_SHARE = 2 / 3


def make_splits(run: Path, seed: int = 0) -> dict[str, Any]:
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    rng = random.Random(f"splits:{seed}")
    start = date.fromisoformat(manifest["start_date"])
    cutoff = start + timedelta(days=round(manifest["days"] * TRAIN_SHARE))
    by_profile: dict[str, list[str]] = {}
    for clinic in manifest["clinics"]:
        by_profile.setdefault(clinic["profile"], []).append(clinic["clinic_id"])
    # Two thirds of all clinics train, shared out over the profiles by largest remainder.
    total = round(len(manifest["clinics"]) * TRAIN_SHARE)
    quotas = {p: len(ids) * TRAIN_SHARE for p, ids in sorted(by_profile.items())}
    counts = {p: int(q) for p, q in quotas.items()}
    for p in sorted(quotas, key=lambda k: (-(quotas[k] - counts[k]), k))[
        : total - sum(counts.values())
    ]:
        counts[p] += 1
    train_clinics: list[str] = []
    for profile, ids in sorted(by_profile.items()):
        ids = sorted(ids)
        rng.shuffle(ids)
        train_clinics += ids[: counts[profile]]
    users = sorted(
        pl.concat([pl.read_parquet(f) for f in (run / "tables" / "users").glob("*.parquet")])[
            "id"
        ].to_list()
    )
    rng.shuffle(users)
    definitions = {
        "seed": seed,
        "temporal": {"test_from": cutoff.isoformat(), "timezone": manifest["timezone"]},
        "clinic": {"train_clinics": sorted(train_clinics)},
        "attack": {
            "test_from": cutoff.isoformat(),
            "timezone": manifest["timezone"],
            "held_out_types": list(HELD_OUT_TYPES),
        },
        "user": {"train_users": sorted(users[: round(len(users) * TRAIN_SHARE)])},
    }
    (run / "splits.json").write_text(json.dumps(definitions, indent=1) + "\n", encoding="utf-8")
    return definitions


def assign(
    events: pl.DataFrame,
    labels: pl.DataFrame,
    split: str,
    definitions: dict[str, Any],
) -> pl.Series:
    """Label each row of `events` (id, clinic_id, user_id, at) as train, test or null (unused)."""
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}")
    d = definitions[split]
    if split in ("temporal", "attack"):
        local_day = pl.col("at").dt.convert_time_zone(d["timezone"]).dt.date()
        is_test = local_day >= date.fromisoformat(d["test_from"])
    elif split == "clinic":
        is_test = ~pl.col("clinic_id").is_in(d["train_clinics"])
    else:
        is_test = ~pl.col("user_id").is_in(d["train_users"])
    frame = events.select("id", "clinic_id", "user_id", "at").with_columns(
        pl.when(is_test).then(pl.lit("test")).otherwise(pl.lit("train")).alias("split")
    )
    if split == "attack":
        frame = frame.join(
            labels.select(pl.col("access_event_id").alias("id"), "attack_type"),
            on="id",
            how="left",
            maintain_order="left",
        ).with_columns(
            pl.when((pl.col("split") == "train") & pl.col("attack_type").is_in(d["held_out_types"]))
            .then(None)
            .otherwise(pl.col("split"))
            .alias("split")
        )
    return frame["split"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write the four SaathiBench splits of a run.")
    parser.add_argument("run", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    definitions = make_splits(args.run, args.seed)
    print(f"wrote {args.run / 'splits.json'}")
    print(f"  temporal and attack: test from {definitions['temporal']['test_from']}")
    print(f"  clinic: {len(definitions['clinic']['train_clinics'])} training clinics")
    print(f"  user: {len(definitions['user']['train_users'])} training users")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
