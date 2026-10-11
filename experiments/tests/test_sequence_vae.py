"""B3's memory fix: the sequences, the sampled training rows and the scores are exactly those of
the implementation it replaced (kept here as the reference)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from e2d_experiments.methods import RAW_FEATURES, SequenceVAE, _x


def reference_sequences(length: int, frame: pl.DataFrame) -> np.ndarray:
    """The implementation before the memory fix, unchanged."""
    ordered = frame.with_row_index("_row").sort("user_id", "at", "access_event_id")
    x = _x(ordered, RAW_FEATURES)
    users = ordered["user_id"].to_numpy()
    seq = np.zeros((len(x), length, x.shape[1]))
    for lag in range(length):
        shifted = np.roll(x, lag, axis=0)
        same = np.roll(users, lag) == users
        same[:lag] = False
        seq[:, length - 1 - lag] = np.where(same[:, None], shifted, 0.0)
    flat = seq.reshape(len(x), -1)
    back = np.empty_like(flat)
    back[ordered["_row"].to_numpy()] = flat
    return back


def accesses(n: int, users: int, seed: int = 0) -> pl.DataFrame:
    """Rows in no particular order, with users of very different sizes and tied times."""
    rng = np.random.default_rng(seed)
    start = datetime(2026, 1, 5, tzinfo=UTC)
    weights = rng.dirichlet(np.ones(users) * 0.3)
    return pl.DataFrame(
        {
            "access_event_id": [f"e{i:06d}" for i in rng.permutation(n)],
            "user_id": [f"u{u}" for u in rng.choice(users, size=n, p=weights)],
            "at": [start + timedelta(minutes=int(m)) for m in rng.integers(0, 600, size=n)],
            **{c: rng.normal(size=n) * (i + 1) for i, c in enumerate(RAW_FEATURES)},
        }
    )


@pytest.mark.parametrize(("n", "users"), [(1, 1), (4, 1), (7, 3), (500, 12), (3000, 40)])
def test_sequences_match_the_reference(n: int, users: int) -> None:
    frame = accesses(n, users)
    vae = SequenceVAE(length=5)
    expected = reference_sequences(5, frame)
    full = vae._sequences(frame)
    assert full.shape == (n, 5 * len(RAW_FEATURES))
    assert np.array_equal(full, expected)
    rows = np.random.default_rng(1).choice(n, size=max(1, n // 3), replace=False)
    assert np.array_equal(vae._sequences(frame, rows), expected[rows]), "a sample, in its order"


def test_training_uses_the_same_rows_and_scores_are_unchanged() -> None:
    train, test = accesses(3000, 40, seed=2), accesses(800, 25, seed=3)
    vae = SequenceVAE(length=5, epochs=1, seed=7, max_rows=1000)
    vae.fit(train)
    # The rows the old code kept: all sequences, then the same generator call.
    old = reference_sequences(5, train)
    kept = old[np.random.default_rng(7).choice(len(old), 1000, replace=False)]
    assert vae.mean is not None and vae.std is not None
    assert np.array_equal(vae.mean, kept.mean(axis=0))
    assert np.array_equal(vae.std, kept.std(axis=0) + 1e-6)

    import torch

    encoder, decoder = vae.model
    data = torch.tensor((reference_sequences(5, test) - vae.mean) / vae.std, dtype=torch.float32)
    with torch.no_grad():
        recon = decoder(encoder(data)[:, :8])
    expected = ((recon - data) ** 2).mean(dim=1).numpy()
    scores = vae.score(test)
    assert scores.shape == (test.height,) and np.isfinite(scores).all()
    assert np.array_equal(scores, expected.astype(float))


def test_small_training_sets_are_not_sampled() -> None:
    train = accesses(300, 5, seed=4)
    vae = SequenceVAE(length=5, epochs=1, seed=0, max_rows=1000)
    vae.fit(train)
    full = reference_sequences(5, train)
    assert vae.mean is not None and np.array_equal(vae.mean, full.mean(axis=0))
