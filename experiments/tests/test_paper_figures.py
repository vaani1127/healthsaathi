"""Every paper figure can be drawn from the committed results, as PDF and 300 DPI PNG."""

from pathlib import Path

import pytest

from e2d_experiments import paper_figures

pytestmark = pytest.mark.skipif(
    not (paper_figures.RESULTS / "none" / "summary.parquet").exists(),
    reason="the registered results are not in this checkout",
)


def test_all_figures_are_written(tmp_path: Path) -> None:
    assert paper_figures.main(["--out", str(tmp_path)]) == 0
    pdfs = sorted(p.stem for p in tmp_path.glob("*.pdf"))
    assert {p.stem for p in tmp_path.glob("*.png")} == set(pdfs)
    for name in ("fig_recall_by_budget", "fig_main_result", "fig_hypotheses", "fig_ablations"):
        assert name in pdfs
    assert all(p.stat().st_size > 10_000 for p in tmp_path.glob("*.png"))
