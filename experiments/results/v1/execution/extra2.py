"""Diagnostics on the temporal split, per seed (no registered result changes): which template explains
the falsely explained attacks, what E2D's benign alerts at B = 5 are, the distribution of explanation
strength for benign accesses and each attack type, and coverage per role. E2D's recall@5 is
recomputed and compared with the registered value.

Run on the execution machine after the final runs, one process per seed:
    python extra2.py <seed index>     # writes /Capstone/extra/diag<i>.json
The ten outputs are combined in ../diagnostics2.json.
"""
import json, sys
from pathlib import Path
import numpy as np, polars as pl
from e2d_core.explain import default_config
from e2d_experiments import evaluate, prepare
from e2d_experiments.evaluate import NON_FORGING_TYPES
from e2d_experiments.methods import make_method
from saathibench.labels import read_labels
from saathibench.splits import assign

BINS = np.linspace(0, 1, 21)

def main() -> None:
    i = int(sys.argv[1]); seed = 20261008 + i
    run = Path(f"/dev/shm/runs/none/v1-s{seed}")
    data = evaluate.with_labels(prepare.prepare(run, workers=1), run)
    expl = pl.concat([pl.read_parquet(f).select("access_event_id", "template_code")
                      for f in sorted((run / "derived" / "explanations-all").glob("*.parquet"))])
    data = data.join(expl, on="access_event_id", how="left", maintain_order="left")
    definitions = json.loads((run / "splits.json").read_text())
    events = data.select(pl.col("access_event_id").alias("id"), "clinic_id", "user_id", "at")
    labels = data.select("access_event_id", "attack_type")
    part = data.with_columns(assign(events, labels, "temporal", definitions).alias("_split"))
    train = part.filter((pl.col("_split") == "train") & (pl.col("role") != "patient"))
    test = evaluate.evaluable(part.filter((pl.col("_split") == "test") & (pl.col("role") != "patient")))
    theta = default_config().theta
    out = {"seed_index": i}
    fe = (test.filter(pl.col("is_attack") & pl.col("attack_type").is_in(NON_FORGING_TYPES) & (pl.col("sigma") >= theta))
          .group_by("attack_type", "template_code").len().sort("attack_type", "len", descending=[False, True]))
    out["falsely_explained_by_template"] = [list(r) for r in fe.iter_rows()]
    benign_all = test.filter(~pl.col("is_attack"))
    out["sigma_hist_benign"] = np.histogram(benign_all["sigma"].fill_null(0).to_numpy(), BINS)[0].tolist()
    out["sigma_hist_by_type"] = {str(t): np.histogram(g["sigma"].fill_null(0).to_numpy(), BINS)[0].tolist()
                                 for (t,), g in test.filter(pl.col("is_attack")).group_by("attack_type")}
    out["coverage_by_role"] = [list(r) for r in benign_all.group_by("role").agg((pl.col("sigma") >= theta).mean(), pl.len()).sort("role").iter_rows()]
    method = make_method("e2d", i, "none"); method.fit(train)
    scored = test.with_columns(pl.Series("score", np.asarray(method.score(test), dtype=float)))
    alerts = (scored.filter(pl.col("score") > 0)
              .sort(["clinic_id", "day", "score", "access_event_id"], descending=[False, False, True, False])
              .group_by(["clinic_id", "day"], maintain_order=True).head(5))
    out["e2d_recall_at_5_recomputed"] = alerts["is_attack"].sum() / test["is_attack"].sum()
    reg = pl.read_parquet("experiments/outputs/v1/none/results.parquet").filter(
        (pl.col("seed") == i) & (pl.col("method") == "e2d") & (pl.col("split") == "temporal")
        & (pl.col("budget") == 5) & (pl.col("metric") == "recall") & pl.col("group").is_null())
    out["e2d_recall_at_5_registered"] = reg["value"][0]
    scen = read_labels(run, "benign_scenarios").select("access_event_id", "scenario").unique("access_event_id")
    benign = alerts.filter(~pl.col("is_attack")).join(scen, on="access_event_id", how="left")
    out["alerts"] = alerts.height
    out["benign_alerts_by_scenario"] = [list(r) for r in benign.group_by(pl.col("scenario").fill_null("ordinary")).len().sort("len", descending=True).iter_rows()]
    out["benign_alerts_by_role"] = [list(r) for r in benign.group_by("role").len().sort("len", descending=True).iter_rows()]
    out["benign_alerts_explained_share"] = float((benign["sigma"] >= theta).mean())
    out["attack_alerts_by_type"] = [list(r) for r in alerts.filter(pl.col("is_attack")).group_by("attack_type").len().sort("attack_type").iter_rows()]
    Path(f"/Capstone/extra/diag{i}.json").write_text(json.dumps(out, default=str))
    print("done seed", i, flush=True)

if __name__ == "__main__":
    main()
