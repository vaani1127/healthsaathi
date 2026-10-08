"""Run an experiment (SPEC 8): every method on every split for every seed, then statistics,
LaTeX tables and figures.

    uv run python -m e2d_experiments.run                       # the smoke config
    uv run python -m e2d_experiments.run data=v1 seeds=[0,1]   # Hydra overrides

Outputs go to `<out>/results.parquet`, `summary.parquet`, `tests.parquet`, `tables/` and
`figures/`. Only a run of the pre-registered final config should export into paper/.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

import hydra
import numpy as np
import polars as pl
from omegaconf import DictConfig, OmegaConf

from e2d_experiments import evaluate, export, prepare, stats
from e2d_experiments.methods import make_method
from saathibench.audit import audit
from saathibench.config import AttackConfig
from saathibench.splits import assign

logger = logging.getLogger(__name__)


def _attack_overrides(cfg: DictConfig) -> dict[str, Any]:
    levels = cfg.ablation.get("mimicry")
    if levels is None:
        return {}
    from saathibench.config import load_config

    base = load_config(Path(cfg.data.sim_config)).attacks
    return {
        "attacks": AttackConfig(
            campaigns_per_clinic_month=base.campaigns_per_clinic_month,
            first_day=base.first_day,
            types=base.types,
            mimicry=tuple(float(m) for m in levels),
        )
    }


def require_audit(run: Path, required: bool = True) -> None:
    """The separability audit must pass before a run is used (SPEC 7). The report is always
    written; only data configs too small for it (smoke) may skip the gate."""
    path = run / "audit.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else audit(run)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["passed"] and not required:
        logger.warning("audit failed but is not required for this data", extra={"run": str(run)})
    elif not report["passed"]:
        raise SystemExit(f"{run} failed the separability audit: {report['failures']}")


def run_seed(cfg: DictConfig, seed: int, out: Path) -> list[dict[str, Any]]:
    name = cfg.data.name + (f"-{cfg.ablation.name}" if cfg.ablation.get("mimicry") else "")
    overrides = _attack_overrides(cfg)
    if cfg.data.get("days"):
        overrides["days"] = int(cfg.data.days)
    run = prepare.simulate_run(
        Path(cfg.data.sim_config), out, name, int(cfg.data.base_seed) + seed, **overrides
    )
    require_audit(run, bool(cfg.data.get("require_audit", True)))
    templates = cfg.ablation.get("templates")
    features = prepare.prepare(
        run, templates=list(templates) if templates else None, workers=cfg.workers
    )
    data = evaluate.with_labels(features, run)
    definitions = json.loads((run / "splits.json").read_text(encoding="utf-8"))
    events = data.select(pl.col("access_event_id").alias("id"), "clinic_id", "user_id", "at")
    labels = data.select("access_event_id", "attack_type")
    budgets = tuple(int(b) for b in cfg.budgets)
    rows: list[dict[str, Any]] = []
    for split in cfg.splits:
        part = data.with_columns(assign(events, labels, split, definitions).alias("_split"))
        train = part.filter((pl.col("_split") == "train") & (pl.col("role") != "patient"))
        test = evaluate.evaluable(part.filter(pl.col("_split") == "test"))
        base = {"seed": seed, "split": split, "ablation": cfg.ablation.name}
        for metric, value in evaluate.method_independent(test).items():
            rows.append(
                {
                    **base,
                    "method": "-",
                    "budget": 0,
                    "metric": metric,
                    "group": None,
                    "value": value,
                }
            )
        y_train = train["is_attack"].to_numpy().astype(int)
        for method_name in cfg.methods:
            started = time.perf_counter()
            method = make_method(
                method_name,
                seed,
                cfg.ablation.name,
                window=cfg.ablation.get("window"),
                train_share=cfg.ablation.get("train_share", 1.0),
                vae_epochs=cfg.data.get("vae_epochs", cfg.vae_epochs),
            )
            method.fit(train, y_train if method_name == "upper_lightgbm" else None)
            scores = np.asarray(method.score(test), dtype=float)
            for row in evaluate.metrics(test, scores, budgets):
                rows.append({**base, "method": method_name, "group": None, **row})
            logger.info(
                "scored",
                extra={"seed": seed, "split": split, "method": method_name,
                       "seconds": round(time.perf_counter() - started, 1)},
            )  # fmt: skip
    return rows


def log_mlflow(cfg: DictConfig, results: pl.DataFrame) -> None:
    import mlflow

    uri = str(cfg.mlflow.tracking_uri)
    if uri.startswith("sqlite:///"):
        Path(uri.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment(cfg.mlflow.experiment)
    plain = results.filter(pl.col("group").is_null() & (pl.col("method") != "-"))
    for (seed, split, method), group in plain.group_by("seed", "split", "method"):
        with mlflow.start_run(
            run_name=f"{cfg.data.name}-{cfg.ablation.name}-{method}-{split}-s{seed}"
        ):
            mlflow.log_params(
                {"data": cfg.data.name, "ablation": cfg.ablation.name, "method": method,
                 "split": split, "seed": seed}
            )  # fmt: skip
            mlflow.log_metrics(
                {
                    f"{r['metric']}_at_{r['budget']}": float(r["value"])
                    for r in group.iter_rows(named=True)
                    if r["value"] == r["value"]
                }
            )


@hydra.main(config_path="../../conf", config_name="config", version_base="1.3")
def main(cfg: DictConfig) -> None:
    out = Path(cfg.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.yaml").write_text(OmegaConf.to_yaml(cfg), encoding="utf-8")
    rows: list[dict[str, Any]] = []
    for seed in cfg.seeds:
        rows += run_seed(cfg, int(seed), out)
    results = pl.DataFrame(rows, schema={
        "seed": pl.Int64, "split": pl.String, "ablation": pl.String, "method": pl.String,
        "budget": pl.Int64, "metric": pl.String, "group": pl.String, "value": pl.Float64,
    })  # fmt: skip
    results.write_parquet(out / "results.parquet")
    summary = stats.summarise(results)
    summary.write_parquet(out / "summary.parquet")
    tests = stats.hypothesis_tests(results, ablation=cfg.ablation.name)
    if tests.height:
        tests.write_parquet(out / "tests.parquet")
    tables = export.write_tables(summary, tests, Path(cfg.export.tables))
    figures = export.write_figures(summary, Path(cfg.export.figures))
    if cfg.mlflow.enabled:
        log_mlflow(cfg, results)
    print(f"wrote {len(tables)} tables and {len(figures)} figures; results in {out}")


if __name__ == "__main__":
    main()
