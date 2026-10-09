"""Before registration: simulate every pre-registered v1 seed, run the separability audit and the
realism measures, and write the results to experiments/audit/v1/ (committed with the
pre-registration). No detection method is fitted.

It also reports, without gating on them, the single-feature ROC-AUC of each forgery flag for
type-10 (forgery) accesses against a sample of benign staff accesses. Those flags come from the
shared explanation engine, evaluated 24 hours after each access as in the experiments.

    uv run python -m e2d_experiments.prereg_audit            # seeds 0..9
"""

import argparse
import json
import multiprocessing
import random
import sys
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.metrics import roc_auc_score

from e2d_core.explain import AccessEvent, EvidenceQuery, default_config, explain
from e2d_core.explain.forgery import FLAG_NAMES
from e2d_core.repo.memory import COLUMNS, MemoryRepository
from e2d_experiments import prepare
from saathibench.audit import audit
from saathibench.coverage import clinic_keys, read
from saathibench.labels import attack_labels
from saathibench.realism import measure

SIM_CONFIG = Path("sim/configs/v1.yaml")
BASE_SEED = 20261008
RUNS = Path("experiments/outputs/v1/none")
FORGERY_TYPE = 10
BENIGN_PER_CLINIC = 1000
# Every boolean flag, plus the self-creation rate that self_creation_high is derived from.
FLAG_SIGNALS = (*FLAG_NAMES, "self_creation_rate")


def _clinic_flags(args: tuple[Path, str, str, list[str]]) -> list[dict[str, Any]]:
    run, key, timezone, chosen = args
    events = read(run, "tables", "access_events", key)
    if events is None or not chosen:
        return []
    events = events.filter(pl.col("id").is_in(chosen))
    frames = {
        t: f.select(c) for t, c in COLUMNS.items() if (f := read(run, "tables", t, key)) is not None
    }
    repo = MemoryRepository(frames)
    config = default_config()
    rows = []
    for row in events.iter_rows(named=True):
        event = AccessEvent(
            id=uuid.UUID(row["id"]),
            clinic_id=uuid.UUID(row["clinic_id"]),
            user_id=uuid.UUID(row["user_id"]),
            role=row["role"],
            patient_id=uuid.UUID(row["patient_id"]),
            resource=row["resource_type"],
            action=row["action"],
            at=row["at"],
            break_glass_id=uuid.UUID(row["break_glass_id"]) if row["break_glass_id"] else None,
        )
        as_of = event.at + prepare.RESCORE_AFTER
        query = EvidenceQuery(
            event.clinic_id, event.user_id, event.patient_id, event.at, timezone, event.role, as_of
        )
        flags = explain(event, repo.evidence_for_sync(query), config, timezone, as_of).forgery_flags
        rows.append({"access_event_id": row["id"], **{f: flags.get(f) for f in FLAG_SIGNALS}})
    return rows


def forgery_flag_auc(run: Path, seed: int) -> dict[str, Any]:
    """Fire rate and ROC-AUC of each forgery signal, type-10 accesses (positive) against a sample of
    benign staff accesses. A flag that is undecided (None) counts as not fired. AUC 0.5 means no
    separation; below 0.5 the flag fires more often for benign accesses."""
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    labels = attack_labels(run).select("access_event_id", "is_attack", "attack_type")
    rng = random.Random(f"flags:{seed}")
    jobs = []
    for key in clinic_keys(run):
        events = read(run, "tables", "access_events", key)
        if events is None:
            continue
        staff = (
            events.filter(pl.col("role") != "patient")
            .select("id")
            .join(labels, left_on="id", right_on="access_event_id")
        )
        forged = staff.filter(pl.col("attack_type") == FORGERY_TYPE)["id"].to_list()
        benign = staff.filter(~pl.col("is_attack"))["id"].to_list()
        sample = rng.sample(benign, min(BENIGN_PER_CLINIC, len(benign)))
        jobs.append((run, key, manifest["timezone"], forged + sample))
    context = multiprocessing.get_context("spawn")
    workers = max(1, min(multiprocessing.cpu_count() - 1, len(jobs)))
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        rows = [r for part in pool.map(_clinic_flags, jobs) for r in part]
    data = pl.DataFrame(rows, infer_schema_length=None).join(
        labels, on="access_event_id", how="left"
    )
    y = (data["attack_type"] == FORGERY_TYPE).fill_null(False).to_numpy().astype(int)
    out: dict[str, Any] = {
        "forged_accesses": int(y.sum()),
        "benign_sampled": int((y == 0).sum()),
        "auc": {},
        "fire_rate_type10": {},
        "fire_rate_benign": {},
    }
    for name in FLAG_SIGNALS:
        x = data[name].cast(pl.Float64).fill_null(0.0).to_numpy()
        if y.min(initial=1) == y.max(initial=0) or len(np.unique(x)) < 2:
            out["auc"][name] = 0.5
        else:
            out["auc"][name] = round(float(roc_auc_score(y, x)), 4)
        if name in FLAG_NAMES:
            fired = x > 0
            out["fire_rate_type10"][name] = (
                round(float(fired[y == 1].mean()), 4) if y.any() else None
            )
            out["fire_rate_benign"][name] = round(float(fired[y == 0].mean()), 4)
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Separability audit of the pre-registered runs.")
    parser.add_argument("--seeds", type=int, default=10)
    parser.add_argument("--out", type=Path, default=Path("experiments/audit/v1"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"sim_config": str(SIM_CONFIG), "runs": []}
    for i in range(args.seeds):
        run = prepare.simulate_run(SIM_CONFIG, RUNS, "v1", BASE_SEED + i)
        report = audit(run)
        report["run"] = run.name
        (run / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (args.out / f"audit-seed{i}.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        realism = measure(run)
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        summary["runs"].append(
            {
                "seed_index": i,
                "simulation_seed": BASE_SEED + i,
                "config_digest": manifest["config_digest"],
                "passed": report["passed"],
                "max_single": report["max_single"],
                "depth2_tree_auc": report["depth2_tree_auc"],
                "realism": realism,
                "forgery_flag_auc_type10_vs_benign": forgery_flag_auc(run, i),
            }
        )
        print(f"seed {i}: audit {'PASS' if report['passed'] else 'FAIL'}", flush=True)
    summary["all_passed"] = all(r["passed"] for r in summary["runs"])
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("all passed" if summary["all_passed"] else "NOT all passed: do not register yet")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
