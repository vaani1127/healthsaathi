"""Recording overhead table (SPEC 8, RQ5) from the load test files in experiments/results/load.

    uv run python -m e2d_experiments.load        # writes paper/tables/load_overhead.tex

Each variant (recording, no-recording) has `<variant>_stats.csv` from Locust (latency per endpoint
in milliseconds) and `<variant>_memory.txt` (the benchmark container's memory every 5 seconds).
"""

import re
import sys
from pathlib import Path

import polars as pl

from e2d_experiments.export import _tabular

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "experiments" / "results" / "load"
VARIANTS = {"recording": "With recording", "no-recording": "Without recording"}
UNITS = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "B": 1 / 1024 / 1024}


def endpoint_stats(folder: Path) -> pl.DataFrame:
    frames = []
    for variant in VARIANTS:
        path = folder / f"{variant}_stats.csv"
        if not path.exists():
            continue
        stats = pl.read_csv(path).filter(pl.col("Name") != "Aggregated")
        frames.append(
            stats.select(
                pl.lit(variant).alias("variant"),
                pl.col("Name").alias("endpoint"),
                pl.col("Request Count").alias("requests"),
                pl.col("Failure Count").alias("failures"),
                pl.col("50%").cast(pl.Float64).alias("p50_ms"),
                pl.col("95%").cast(pl.Float64).alias("p95_ms"),
                pl.col("Requests/s").cast(pl.Float64).alias("rps"),
            )
        )
    return pl.concat(frames) if frames else pl.DataFrame()


def memory_mib(text: str) -> list[float]:
    """`docker stats` lines such as "245.3MiB / 887.1MiB": the used part, in MiB."""
    values = []
    for line in text.splitlines():
        match = re.match(r"\s*([\d.]+)\s*(KiB|MiB|GiB|B)\s*/", line)
        if match:
            values.append(float(match.group(1)) * UNITS[match.group(2)])
    return values


def memory_stats(folder: Path) -> dict[str, tuple[float, float]]:
    out = {}
    for variant in VARIANTS:
        path = folder / f"{variant}_memory.txt"
        if path.exists():
            values = memory_mib(path.read_text(encoding="utf-8"))
            if values:
                ordered = sorted(values)
                out[variant] = (ordered[len(ordered) // 2], ordered[-1])
    return out


def table(stats: pl.DataFrame, memory: dict[str, tuple[float, float]], setup: str) -> str:
    rows = []
    for endpoint in sorted(stats["endpoint"].unique().to_list()):
        for variant, label in VARIANTS.items():
            row = stats.filter((pl.col("variant") == variant) & (pl.col("endpoint") == endpoint))
            if row.is_empty():
                continue
            r = row.to_dicts()[0]
            rows.append(
                [
                    endpoint,
                    label,
                    f"{r['requests']:,}",
                    f"{r['failures']:,}",
                    f"{r['p50_ms']:.0f}",
                    f"{r['p95_ms']:.0f}",
                ]
            )
    for variant, label in VARIANTS.items():
        if variant in memory:
            median, peak = memory[variant]
            rows.append(
                [f"API memory (MiB), {label.lower()}", "", "", "", f"{median:.0f}", f"{peak:.0f}"]
            )
    header = ["Endpoint", "Variant", "Requests", "Failures", "P50 (ms)", "P95 (ms)"]
    caption = (
        "Latency with and without access recording (explanation and audit logging), on a copy "
        f"of the API with its own database. Memory rows: median and peak. {setup}"
    )
    return _tabular(header, rows, caption, "tab:load-overhead")


def write(folder: Path = RESULTS, tables: Path = ROOT / "paper" / "tables") -> Path:
    stats = endpoint_stats(folder)
    if stats.is_empty():
        raise SystemExit(f"no load test results in {folder}")
    setup_file = folder / "setup.txt"
    setup = ""
    if setup_file.exists():
        info = dict(
            line.split("=", 1)
            for line in setup_file.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        users, duration = info.get("users", "?"), info.get("duration", "?")
        setup = f"{users} simulated users for {duration} on {info.get('vm', 'the VM')}."
    tables.mkdir(parents=True, exist_ok=True)
    out = tables / "load_overhead.tex"
    out.write_text(table(stats, memory_stats(folder), setup), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(f"wrote {write()}")
    sys.exit(0)
