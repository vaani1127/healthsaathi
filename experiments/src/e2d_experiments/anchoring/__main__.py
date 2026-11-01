import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from omegaconf import OmegaConf

from e2d_experiments.anchoring import bench, collect, report

ROOT = Path(__file__).resolve().parents[4]
CONFIG = ROOT / "experiments" / "configs" / "anchoring.yaml"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m e2d_experiments.anchoring")
    parser.add_argument("step", choices=["snapshot", "enrich", "bench", "report"])
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
    else:
        gwei = [float(g) for g in cfg.mainnet_gwei]
        for path in report.write(
            folder, ROOT / "paper" / "tables", ROOT / "paper" / "figures", gwei
        ):
            print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
