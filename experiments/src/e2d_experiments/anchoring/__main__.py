import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
from omegaconf import OmegaConf

from e2d_experiments.anchoring import bench, cadence, collect, report

ROOT = Path(__file__).resolve().parents[4]
CONFIG = ROOT / "experiments" / "configs" / "anchoring.yaml"
WEB_BENCH = ROOT / "apps" / "web" / "public" / "bench" / "receipts.json"
STEPS = ["snapshot", "enrich", "bench", "bench-receipts", "cadence", "report"]


def chain_probe(folder: Path) -> dict[str, str] | None:
    """A live clinic and one of its confirmed Amoy anchors, from the snapshot."""
    path = folder / "enriched.parquet"
    if not path.exists():
        return None
    rows = (
        pl.read_parquet(path)
        .filter((pl.col("backend") == "amoy") & (pl.col("status") == "confirmed"))
        .sort("submitted_at")
    )
    if rows.is_empty():
        return None
    row = rows.row(-1, named=True)
    clinic = row["clinic_id"].replace("-", "")
    return {"chain": "amoy", "clinic_id_bytes32": "0x" + clinic + "0" * 32, "tx_ref": row["tx_ref"]}


def write_cadence(folder: Path, cfg: Any) -> list[Path]:
    """Anchors per day and gaps between anchors by study period, from the snapshot's receipts
    (counts and times only), as CSV next to the snapshot."""
    receipts = pl.read_parquet(folder / "receipts.parquet")
    periods = cadence.periods_from_config(cfg.periods)
    tz = str(cfg.clinic_timezone)
    sessions = cadence.sessions_from_config(cfg.opd_sessions)
    out = folder.parent / "cadence"
    out.mkdir(parents=True, exist_ok=True)
    per_day = cadence.anchors_per_day(receipts, tz, periods)
    gaps = cadence.anchor_gaps(receipts, periods, tz, sessions)
    frames = {
        "anchors_per_day.csv": per_day,
        "daily_summary.csv": cadence.daily_summary(per_day),
        "gap_summary.csv": cadence.gap_summary(gaps),
    }
    for name, frame in frames.items():
        frame.write_csv(out / name)
    return [out / name for name in frames]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m e2d_experiments.anchoring")
    parser.add_argument("step", choices=STEPS)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--snapshot-dir", type=Path, help="overrides snapshot_dir in the config")
    args = parser.parse_args(argv)
    cfg = OmegaConf.load(args.config)
    folder = ROOT / (args.snapshot_dir or Path(str(cfg.snapshot_dir)))

    if args.step == "snapshot":
        url = os.environ.get("ANCHOR_STUDY_DATABASE_URL")
        if not url:
            raise SystemExit("set ANCHOR_STUDY_DATABASE_URL (admin login; read-only transaction)")
        counts = collect.run_snapshot(url, datetime.fromisoformat(str(cfg.since)), folder)
        print(f"snapshot in {folder}: {counts}")
    elif args.step == "enrich":
        rpc = {str(k): str(v) for k, v in cfg.rpc.items()}
        final = collect.run_enrich(folder, rpc, str(cfg.bitcoin_api))
        print(f"{final} confirmed receipts have a final time")
    elif args.step == "bench":
        out = folder.parent / "bench"
        out.mkdir(parents=True, exist_ok=True)
        sizes = [int(n) for n in cfg.bench.tree_sizes]
        bench.proof_sizes(sizes).write_parquet(out / "proofs.parquet")
        bench.build_times(sizes, int(cfg.bench.repeats)).write_parquet(out / "builds.parquet")
        (out / "machine.txt").write_text(bench.machine() + "\n", encoding="utf-8")
        print(f"bench in {out}")
    elif args.step == "bench-receipts":
        sizes = [int(n) for n in cfg.bench.tree_sizes]
        data = bench.bench_file(sizes, chain_probe(folder))
        WEB_BENCH.parent.mkdir(parents=True, exist_ok=True)
        WEB_BENCH.write_bytes((json.dumps(data, indent=1) + "\n").encode())
        probe = data["chain_probe"] is not None
        print(f"wrote {WEB_BENCH.relative_to(ROOT)} (chain probe: {probe})")
    elif args.step == "cadence":
        for path in write_cadence(folder, cfg):
            print(f"wrote {path}")
    else:
        gwei = [float(g) for g in cfg.mainnet_gwei]
        for path in report.write(
            folder,
            ROOT / "paper" / "tables",
            ROOT / "paper" / "figures",
            gwei,
            cadence.periods_from_config(cfg.periods),
            str(cfg.clinic_timezone),
            cadence.sessions_from_config(cfg.opd_sessions),
        ):
            print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
