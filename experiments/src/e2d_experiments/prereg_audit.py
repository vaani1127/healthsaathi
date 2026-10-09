"""Before registration: simulate every pre-registered v1 seed, run the separability audit and the
realism measures, and write the results to experiments/audit/v1/ (committed with the
pre-registration). No method is fitted and no explanation is computed.

    uv run python -m e2d_experiments.prereg_audit            # seeds 0..9
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from e2d_experiments import prepare
from saathibench.audit import audit
from saathibench.realism import measure

SIM_CONFIG = Path("sim/configs/v1.yaml")
BASE_SEED = 20261008
RUNS = Path("experiments/outputs/v1/none")


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
            }
        )
        print(f"seed {i}: audit {'PASS' if report['passed'] else 'FAIL'}", flush=True)
    summary["all_passed"] = all(r["passed"] for r in summary["runs"])
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("all passed" if summary["all_passed"] else "NOT all passed: do not register yet")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
