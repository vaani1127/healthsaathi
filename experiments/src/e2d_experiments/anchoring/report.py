"""LaTeX tables and figures for the anchoring study, from the committed snapshot and bench files."""

from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

from e2d_experiments.anchoring import analysis
from e2d_experiments.export import _tabular

BACKEND_NAMES = {
    "amoy": "Polygon Amoy",
    "besu": "Besu QBFT (4 nodes)",
    "github": "GitHub witness",
    "ots": "OpenTimestamps (Bitcoin)",
}


def _name(backend: str) -> str:
    return BACKEND_NAMES.get(backend, backend)


def _num(value: float | int | None, digits: int = 1) -> str:
    if value is None or value != value:
        return "--"
    return f"{value:,.{digits}f}"


def _period(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "no events yet"
    return f"{start:%d %b %Y} to {end:%d %b %Y}"


def windows_table(summary: pl.DataFrame, period: str) -> str:
    rows = [
        [
            _name(r["backend"]),
            f"{r['events']:,}",
            f"{r['censored']:,}",
            _num(r["median_min"]),
            _num(r["p90_min"]),
            _num(r["p95_min"]),
            _num(r["max_min"]),
        ]
        for r in summary.to_dicts()
    ]
    header = ["Backend", "Events", "Not yet covered", "Median", "P90", "P95", "Max"]
    caption = (
        "Rewrite window per witness in minutes: from an audit event being written until a final "
        f"witness record covers it. Live prod-demo, {period}."
    )
    return _tabular(header, rows, caption, "tab:rewrite-window")


def parts_table(summary: pl.DataFrame, period: str) -> str:
    rows = [
        [
            _name(r["backend"]),
            f"{r['checkpoints']:,}",
            _num(r["job_delay_median_min"]),
            _num(r["finality_median_min"]),
            _num(r["finality_p95_min"]),
        ]
        for r in summary.to_dicts()
    ]
    header = ["Backend", "Checkpoints", "Job delay (median)", "Finality (median)", "Finality (P95)"]
    caption = (
        "Parts of the rewrite window in minutes: delay of the anchor run after its 15 minute slot, "
        f"and time from the signed checkpoint to the final witness record. {period}."
    )
    return _tabular(header, rows, caption, "tab:rewrite-parts")


def gas_table(summary: pl.DataFrame, mainnet_gwei: list[float]) -> str:
    rows = []
    for r in summary.to_dicts():
        rows.append(
            [
                _name(r["backend"]),
                f"{r['anchors']:,}",
                f"{r['gas_median']:,.0f}",
                f"{r['gas_min']:,.0f}--{r['gas_max']:,.0f}",
                _num(r["fee_paid_median"], 6),
                *(_num(r[f"mainnet_eth_at_{g:g}_gwei"], 6) for g in mainnet_gwei),
            ]
        )
    header = [
        "Chain",
        "Anchors",
        "Gas (median)",
        "Gas (range)",
        "Fee paid (median, native token)",
        *(f"Mainnet ETH at {g:g} gwei (estimate)" for g in mainnet_gwei),
    ]
    caption = (
        "Gas per anchor transaction. The mainnet columns are estimates: median gas times a stated "
        "gas price, not measured."
    )
    return _tabular(header, rows, caption, "tab:anchor-gas")


def proofs_table(proofs: pl.DataFrame, builds: pl.DataFrame, machine: str) -> str:
    joined = proofs.join(builds, on="tree_size", how="left")
    rows = [
        [
            f"{r['tree_size']:,}",
            str(r["inclusion_hashes"]),
            f"{r['inclusion_bytes']:,}",
            f"{r['consistency_bytes']:,}",
            f"{r['sth_bytes']:,}",
            _num(r["median_s"] * 1000 if r["median_s"] is not None else None, 1),
        ]
        for r in joined.to_dicts()
    ]
    header = ["Leaves", "Proof hashes", "Inclusion (B)", "Consistency (B)", "STH (B)", "Build (ms)"]
    caption = (
        "Proof and signed tree head sizes, and median time to build the Merkle tree from leaf "
        f"hashes (e2d-core, in memory) on {machine}."
    )
    return _tabular(header, rows, caption, "tab:proof-size")


def window_figure(windows: pl.DataFrame, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    for backend in sorted(windows["backend"].unique().to_list()):
        values = (
            windows.filter((pl.col("backend") == backend) & pl.col("window_s").is_not_null())
            .get_column("window_s")
            .sort()
            / 60
        )
        if values.is_empty():
            continue
        share = [(i + 1) / len(values) for i in range(len(values))]
        ax.step(values.to_list(), share, where="post", label=_name(backend))
    ax.set_xscale("log")
    ax.set_xlabel("Rewrite window (minutes, log scale)")
    ax.set_ylabel("Share of audit events")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(out / f"fig_rewrite_window.{ext}", dpi=300)
    plt.close(fig)


def write(snapshot: Path, tables: Path, figures: Path, mainnet_gwei: list[float]) -> list[Path]:
    events = pl.read_parquet(snapshot / "events.parquet")
    checkpoints = pl.read_parquet(snapshot / "checkpoints.parquet")
    receipts = pl.read_parquet(snapshot / "enriched.parquet")
    period = _period(*analysis.study_period(events))
    windows = analysis.event_windows(events, checkpoints, receipts)
    parts = analysis.checkpoint_parts(checkpoints, receipts)
    tables.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    written = {
        tables / "anchoring_windows.tex": windows_table(analysis.window_summary(windows), period),
        tables / "anchoring_parts.tex": parts_table(analysis.parts_summary(parts), period),
        tables / "anchoring_gas.tex": gas_table(
            analysis.gas_summary(receipts, mainnet_gwei), mainnet_gwei
        ),
    }
    bench = snapshot.parent / "bench"
    if (bench / "proofs.parquet").exists():
        machine = (bench / "machine.txt").read_text(encoding="utf-8").strip()
        written[tables / "anchoring_proofs.tex"] = proofs_table(
            pl.read_parquet(bench / "proofs.parquet"),
            pl.read_parquet(bench / "builds.parquet"),
            machine,
        )
    for path, text in written.items():
        path.write_text(text, encoding="utf-8")
    window_figure(windows, figures)
    return [*written, figures / "fig_rewrite_window.pdf"]
