"""LaTeX tables and figures for the anchoring study, from the committed snapshot and bench files."""

import json
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

from e2d_experiments.anchoring import analysis, cadence
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


BENCH_PERIOD = "bench"


def _in_period_order(summary: pl.DataFrame, labels: dict[str, str]) -> list[dict[str, Any]]:
    """Rows in the order the periods are configured, then by backend; rows outside every period
    (null) are left out."""
    order = {name: i for i, name in enumerate(labels)}
    rows = [r for r in summary.to_dicts() if r["period"] in order]
    return sorted(rows, key=lambda r: (order[r["period"]], r["backend"]))


def windows_table(summary: pl.DataFrame, labels: dict[str, str], span: str) -> str:
    rows = [
        [
            labels[r["period"]],
            _name(r["backend"]),
            f"{r['events']:,}",
            f"{r['censored']:,}",
            _num(r["median_min"]),
            _num(r["p90_min"]),
            _num(r["p95_min"]),
            _num(r["max_min"]),
        ]
        for r in _in_period_order(summary, labels)
    ]
    header = ["Period", "Backend", "Events", "Not yet covered", "Median", "P90", "P95", "Max"]
    caption = (
        "Rewrite window per witness in minutes: from an audit event being written until a final "
        f"witness record covers it, by study period (event time). Live prod-demo, {span}."
    )
    return _tabular(header, rows, caption, "tab:rewrite-window")


def parts_table(summary: pl.DataFrame, labels: dict[str, str], span: str) -> str:
    rows = [
        [
            labels[r["period"]],
            _name(r["backend"]),
            f"{r['checkpoints']:,}",
            _num(r["job_delay_median_min"]),
            _num(r["finality_median_min"]),
            _num(r["finality_p95_min"]),
        ]
        for r in _in_period_order(summary, labels)
    ]
    header = [
        "Period",
        "Backend",
        "Checkpoints",
        "Job delay (median)",
        "Finality (median)",
        "Finality (P95)",
    ]
    caption = (
        "Parts of the rewrite window in minutes: delay of the anchor run after its 15 minute slot, "
        "and time from the signed checkpoint to the final witness record, by study period "
        f"(checkpoint time). {span}."
    )
    return _tabular(header, rows, caption, "tab:rewrite-parts")


def cadence_table(daily: pl.DataFrame, gaps: pl.DataFrame, labels: dict[str, str]) -> str:
    """Anchors per clinic-day and gaps between anchors, by study period (cadence.py)."""
    gap = {(r["period"], r["backend"], r["scope"]): r for r in gaps.to_dicts()}
    empty: dict[str, Any] = {}
    rows = []
    for r in _in_period_order(daily, labels):
        key = (r["period"], r["backend"])
        every = gap.get((*key, "all"), empty)
        inside = gap.get((*key, "inside one OPD session"), empty)
        rows.append(
            [
                labels[r["period"]],
                _name(r["backend"]),
                f"{r['clinic_days']:,}",
                _num(r["median_per_day"]),
                _num(every.get("median_min")),
                _num(every.get("max_min")),
                _num(inside.get("median_min")),
                _num(inside.get("max_min")),
            ]
        )
    header = [
        "Period",
        "Backend",
        "Clinic-days",
        "Anchors per day (median)",
        "Gap median",
        "Gap max",
        "Gap median in OPD",
        "Gap max in OPD",
    ]
    caption = (
        "Anchoring cadence on the live demo by study period: anchors per clinic and day (clinic "
        "time), and minutes between consecutive anchors of a clinic, for all pairs and for pairs "
        "inside one OPD session. From e2d\\_experiments.anchoring.cadence."
    )
    return _tabular(header, rows, caption, "tab:anchor-cadence")


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


def browser_rows(folder: Path) -> list[dict[str, Any]]:
    """Results downloaded from /bench/receipt, one JSON file per device."""
    rows = []
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        chain = data.get("chain") or {}
        for r in data["offline"]:
            rows.append(
                {
                    "device": data["label"] or path.stem,
                    "tree_size": r["tree_size"],
                    "proof_hashes": r["proof_hashes"],
                    "median_ms": r["median_ms"],
                    "p95_ms": r["p95_ms"],
                    "verified": r["verified"],
                    "chain_median_ms": chain.get("median_ms"),
                }
            )
    return rows


def browser_table(rows: list[dict[str, Any]]) -> str:
    if any(not r["verified"] for r in rows):
        raise ValueError("a bench receipt did not verify; the timings are not valid")
    body = [
        [
            r["device"],
            f"{r['tree_size']:,}",
            str(r["proof_hashes"]),
            _num(r["median_ms"], 2),
            _num(r["p95_ms"], 2),
            _num(r["chain_median_ms"], 0),
        ]
        for r in rows
    ]
    header = ["Device", "Leaves", "Proof hashes", "Offline (median ms)", "Offline (P95 ms)",
              "Chain reads (median ms)"]  # fmt: skip
    caption = (
        "Receipt check time in the browser: offline checks (payload, inclusion proof, tree head "
        "signature) and the chain reads of a real check against Polygon Amoy (/bench/receipt)."
    )
    return _tabular(header, body, caption, "tab:receipt-time")


TAMPER_CHECKS = {
    "audit_page": ("Admin audit page", "when an admin opens it"),
    "next_checkpoint": ("Next checkpoint", "next anchor run after a new event"),
    "chain": ("Root on chain", "next anchor run after a new event"),
    "witness": ("Published heads (verify\\_witness)", "when an auditor runs it"),
}
TAMPER_COLUMNS = {
    "control": "No attack",
    "events": "Rows rewritten",
    "events_and_checkpoints": "Rows rewritten, checkpoints deleted",
}


def tamper_table(data: dict[str, Any]) -> str:
    results = data["results"]
    rows = [
        [
            label,
            when,
            *("caught" if results[col][key]["detected"] else "--" for col in TAMPER_COLUMNS),
        ]
        for key, (label, when) in TAMPER_CHECKS.items()
    ]
    header = ["Check", "Runs", *TAMPER_COLUMNS.values()]
    caption = (
        "A database owner rewrites the audit log and recomputes the hash chain: which check "
        f"catches it. Run of app.scripts.tamper\\_study ({data['setup']})."
    )
    return _tabular(header, rows, caption, "tab:tamper")


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


def write(
    snapshot: Path,
    tables: Path,
    figures: Path,
    mainnet_gwei: list[float],
    periods: list[cadence.Period] | None = None,
    clinic_timezone: str = "Asia/Kolkata",
    sessions: dict[int, list[tuple[time, time]]] | None = None,
) -> list[Path]:
    """Tables and the figure. `periods` splits the live results (configs/anchoring.yaml); without
    it the whole snapshot is one period."""
    events = pl.read_parquet(snapshot / "events.parquet")
    checkpoints = pl.read_parquet(snapshot / "checkpoints.parquet")
    receipts = pl.read_parquet(snapshot / "enriched.parquet")
    span = _period(*analysis.study_period(events))
    if periods is None:
        start = analysis.study_period(events)[0] or datetime(1970, 1, 1, tzinfo=UTC)
        periods = [cadence.Period("all", "Whole study", start, None)]
    labels = {p.name: p.label for p in periods}
    live_receipts = receipts
    windows = cadence.with_period(
        analysis.event_windows(events, checkpoints, receipts), "at", periods
    )
    # The Besu bench has no live events, so it adds to finality and gas only.
    besu = snapshot.parent / "besu"
    if (besu / "enriched.parquet").exists():
        checkpoints = pl.concat(
            [checkpoints, pl.read_parquet(besu / "checkpoints.parquet")], how="vertical_relaxed"
        )
        receipts = pl.concat(
            [receipts, pl.read_parquet(besu / "enriched.parquet")], how="diagonal_relaxed"
        )
    # Bench anchors were sent back to back at some time; they get their own row, not a period.
    parts = cadence.with_period(
        analysis.checkpoint_parts(checkpoints, receipts), "signed_at", periods
    ).with_columns(
        pl.when(pl.col("backend").is_in(analysis.BENCH_BACKENDS))
        .then(pl.lit(BENCH_PERIOD))
        .otherwise(pl.col("period"))
        .alias("period")
    )
    part_labels = {**labels, BENCH_PERIOD: "Bench run"}
    tables.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    written = {
        tables / "anchoring_windows.tex": windows_table(
            analysis.window_summary(windows), labels, span
        ),
        tables / "anchoring_parts.tex": parts_table(
            analysis.parts_summary(parts), part_labels, span
        ),
        tables / "anchoring_gas.tex": gas_table(
            analysis.gas_summary(receipts, mainnet_gwei), mainnet_gwei
        ),
    }
    if "submitted_at" in live_receipts.columns and "clinic_id" in live_receipts.columns:
        sessions = sessions or cadence.sessions_from_config(cadence.DEMO_SESSIONS)
        written[tables / "anchoring_cadence.tex"] = cadence_table(
            cadence.daily_summary(cadence.anchors_per_day(live_receipts, clinic_timezone, periods)),
            cadence.gap_summary(
                cadence.anchor_gaps(live_receipts, periods, clinic_timezone, sessions)
            ),
            labels,
        )
    bench = snapshot.parent / "bench"
    if (bench / "proofs.parquet").exists():
        machine = (bench / "machine.txt").read_text(encoding="utf-8").strip()
        written[tables / "anchoring_proofs.tex"] = proofs_table(
            pl.read_parquet(bench / "proofs.parquet"),
            pl.read_parquet(bench / "builds.parquet"),
            machine,
        )
    tamper = snapshot.parent / "tamper" / "detection.json"
    if tamper.exists():
        written[tables / "anchoring_tamper.tex"] = tamper_table(
            json.loads(tamper.read_text(encoding="utf-8"))
        )
    browser = browser_rows(snapshot.parent / "browser")
    if browser:
        written[tables / "anchoring_browser.tex"] = browser_table(browser)
    for path, text in written.items():
        path.write_text(text, encoding="utf-8")
    window_figure(windows, figures)
    return [*written, figures / "fig_rewrite_window.pdf"]
