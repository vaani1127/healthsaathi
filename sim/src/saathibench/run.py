"""Run SaathiBench: simulate every clinic in a config and write Parquet tables.

    uv run python -m saathibench.run sim/configs/v1.yaml --out sim/output/v1

Each clinic has its own seed derived from the run seed and the clinic's position, so clinics can
run in parallel and the output does not depend on the number of workers. The output folder gets
`tables/` (product table shapes), `labels/` (kept apart from features) and `manifest.json`.
"""

import argparse
import json
import multiprocessing
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from saathibench import __version__
from saathibench.clinic import ClinicSim
from saathibench.config import ClinicSpec, RunConfig, load_config
from saathibench.policy import load_policy


def run_clinic(args: tuple[ClinicSpec, RunConfig, Path]) -> dict[str, Any]:
    spec, cfg, out = args
    started = time.perf_counter()
    sim = ClinicSim(spec, cfg, out, load_policy())
    counts = sim.run()
    return {
        "clinic": spec.index,
        "clinic_id": sim.clinic_id,
        "profile": spec.profile.name,
        "rows": counts,
        "seconds": round(time.perf_counter() - started, 1),
    }


def simulate(
    cfg: RunConfig, out: Path, workers: int | None = None, clinics: int | None = None
) -> dict[str, Any]:
    if clinics is not None:
        cfg = replace(cfg, clinics=cfg.clinics[:clinics])
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    jobs = [(spec, cfg, out) for spec in cfg.clinics]
    workers = max(1, min(workers or (os.cpu_count() or 2) - 1, len(jobs)))
    started = time.perf_counter()
    if workers == 1:
        results = [run_clinic(job) for job in jobs]
    else:
        # spawn, not fork: forking after polars has started its thread pool can deadlock.
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            results = list(pool.map(run_clinic, jobs))
    totals: dict[str, int] = {}
    for result in results:
        for table, n in result["rows"].items():
            totals[table] = totals.get(table, 0) + n
    manifest = {
        "name": cfg.name,
        "saathibench_version": __version__,
        "seed": cfg.seed,
        "start_date": cfg.start_date.isoformat(),
        "days": cfg.days,
        "timezone": cfg.timezone,
        "policy_version": load_policy().sha256,
        "clinics": results,
        "totals": dict(sorted(totals.items())),
        "workers": workers,
        "wall_seconds": round(time.perf_counter() - started, 1),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the SaathiBench simulator.")
    parser.add_argument("config", type=Path)
    parser.add_argument("--out", type=Path, help="output folder (default: sim/output/<name>)")
    parser.add_argument("--workers", type=int, help="parallel clinics (default: CPUs - 1)")
    parser.add_argument("--clinics", type=int, help="only the first N clinics, for quick tries")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    out = args.out or Path("sim/output") / cfg.name
    manifest = simulate(cfg, out, args.workers, args.clinics)
    print(f"wrote {out} in {manifest['wall_seconds']} s with {manifest['workers']} workers")
    for table, n in manifest["totals"].items():
        print(f"  {table:24} {n:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
