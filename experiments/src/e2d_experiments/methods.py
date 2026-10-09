"""Detection methods compared in the paper (SPEC 8). Each fits on training accesses and returns a
score per test access (higher is more suspicious). Only the supervised upper bound sees labels,
and it receives them as an argument; this module never reads label files.

- B0 rules: fixed rules on raw behaviour (night, new device, bursts of exports or patients).
- B1 / B2: IsolationForest on raw features, one global model or one per role.
- B3: a small variational autoencoder over each user's last accesses; reconstruction error.
- B4: co-access collaborative filtering: how unlike the user are the people who usually open
  this patient.
- B5: explanation only: unexplained accesses first, latest first.
- E2D: the gate and per-clinic scorer from e2d-core over every residual feature group.
- Upper bound: LightGBM trained on labelled training attacks (a ceiling, not a competitor).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import polars as pl

from e2d_core.detect import ModelBundle, fit, fit_per_clinic, gate
from e2d_core.explain import default_config
from e2d_core.features import (
    BEHAVIOUR_FEATURES,
    EXPLANATION_FEATURES,
    FORGERY_FEATURES,
    GRAPH_FEATURES,
    ROLE_FEATURES,
)
from e2d_core.features.columns import BEHAVIOUR_COLUMNS, CONTEXT_COLUMNS

RAW_FEATURES = BEHAVIOUR_COLUMNS
WINDOWS = ("1h", "24h", "7d")


# Forgery flags are computed 24 hours after the access (nightly rescoring), so a method that
# uses them can raise its alert only then.
FORGERY_DELAY_HOURS = 24.0


class Method(Protocol):
    name: str
    feature_delay_hours: float

    def fit(self, train: pl.DataFrame, labels: np.ndarray | None = None) -> None: ...

    def score(self, test: pl.DataFrame) -> np.ndarray: ...


def _x(frame: pl.DataFrame, columns: Sequence[str]) -> np.ndarray:
    return frame.select([pl.col(c).cast(pl.Float64).fill_null(0.0) for c in columns]).to_numpy()


@dataclass
class RulesB0:
    name: str = "b0_rules"
    feature_delay_hours: float = 0.0

    def fit(self, train: pl.DataFrame, labels: np.ndarray | None = None) -> None:
        return None

    def score(self, test: pl.DataFrame) -> np.ndarray:
        night = (pl.col("hour_local") < 7) | (pl.col("hour_local") >= 21)
        expr = (
            night.cast(pl.Float64)
            + pl.col("off_shift").cast(pl.Float64)
            + pl.col("new_device").cast(pl.Float64)
            + (pl.col("exports_1h") >= 10).cast(pl.Float64)
            + (pl.col("patients_1h") >= 20).cast(pl.Float64)
            + pl.col("refused").cast(pl.Float64)
        )
        return test.select(expr.alias("s"))["s"].to_numpy()


@dataclass
class IForest:
    """B1 (one model) or B2 (one per role) on raw features."""

    per_role: bool = False
    seed: int = 0
    max_rows: int = 200_000
    name: str = "b1_iforest"
    feature_delay_hours: float = 0.0
    models: dict[str, ModelBundle] = field(default_factory=dict)

    def fit(self, train: pl.DataFrame, labels: np.ndarray | None = None) -> None:
        groups = train.partition_by("role", as_dict=True) if self.per_role else {("all",): train}
        for key, rows in groups.items():
            if rows.height > self.max_rows:
                rows = rows.sample(n=self.max_rows, seed=self.seed)
            self.models[str(key[0])] = fit(rows, RAW_FEATURES, "iforest", self.seed)

    def score(self, test: pl.DataFrame) -> np.ndarray:
        out = np.zeros(test.height)
        roles = test["role"].to_numpy() if self.per_role else np.full(test.height, "all")
        fallback = next(iter(self.models.values()))
        for role in np.unique(roles):
            rows = roles == role
            model = self.models.get(str(role), fallback)
            out[rows] = model.score(test.filter(pl.Series(rows)))
        return out


@dataclass
class ExplanationOnlyB5:
    name: str = "b5_explanation_only"
    feature_delay_hours: float = 0.0

    def fit(self, train: pl.DataFrame, labels: np.ndarray | None = None) -> None:
        return None

    def score(self, test: pl.DataFrame) -> np.ndarray:
        theta = default_config().theta
        recency = pl.col("hour_local") / 24.0
        return test.select(
            pl.when(pl.col("sigma") < theta).then(1.0 + recency).otherwise(0.0).alias("s")
        )["s"].to_numpy()


@dataclass
class CoAccessB4:
    """1 - the largest cosine similarity between the user and anyone who opened the patient in
    training (users compared by the sets of patients they opened, within the clinic)."""

    name: str = "b4_coaccess"
    feature_delay_hours: float = 0.0
    clinics: dict[str, Any] = field(default_factory=dict)

    def fit(self, train: pl.DataFrame, labels: np.ndarray | None = None) -> None:
        pairs = train.select("clinic_id", "user_id", "patient_id").unique()
        for (clinic,), part in pairs.partition_by("clinic_id", as_dict=True).items():
            users = {u: i for i, u in enumerate(sorted(part["user_id"].unique().to_list()))}
            patients = {p: i for i, p in enumerate(sorted(part["patient_id"].unique().to_list()))}
            opened = np.zeros((len(patients), len(users)))
            opened[
                [patients[p] for p in part["patient_id"]], [users[u] for u in part["user_id"]]
            ] = 1.0
            counts = opened.sum(axis=0)
            overlap = opened.T @ opened
            similarity = overlap / np.sqrt(np.outer(counts, counts)).clip(min=1.0)
            self.clinics[str(clinic)] = (users, patients, opened, similarity)

    def score(self, test: pl.DataFrame) -> np.ndarray:
        out = np.ones(test.height)
        clinic_of = test["clinic_id"].to_numpy()
        for clinic in np.unique(clinic_of):
            found = self.clinics.get(str(clinic))
            if found is None:
                continue
            users, patients, opened, similarity = found
            rows = np.flatnonzero(clinic_of == clinic)
            part = test[rows]
            u = np.array([users.get(x, -1) for x in part["user_id"]])
            p = np.array([patients.get(x, -1) for x in part["patient_id"]])
            known = (u >= 0) & (p >= 0)
            best = (similarity[u[known]] * opened[p[known]]).max(axis=1)
            out[rows[known]] = 1.0 - best
        return out


@dataclass
class SequenceVAE:
    """B3: a small VAE over the raw features of each user's last `length` accesses."""

    length: int = 5
    epochs: int = 3
    seed: int = 0
    max_rows: int = 100_000
    name: str = "b3_vae"
    feature_delay_hours: float = 0.0
    model: Any = None
    mean: np.ndarray | None = None
    std: np.ndarray | None = None

    def _sequences(self, frame: pl.DataFrame) -> np.ndarray:
        ordered = frame.with_row_index("_row").sort("user_id", "at", "access_event_id")
        x = _x(ordered, RAW_FEATURES)
        users = ordered["user_id"].to_numpy()
        seq = np.zeros((len(x), self.length, x.shape[1]))
        for lag in range(self.length):
            shifted = np.roll(x, lag, axis=0)
            same = np.roll(users, lag) == users
            same[:lag] = False
            seq[:, self.length - 1 - lag] = np.where(same[:, None], shifted, 0.0)
        flat = seq.reshape(len(x), -1)
        back = np.empty_like(flat)
        back[ordered["_row"].to_numpy()] = flat
        return back

    def fit(self, train: pl.DataFrame, labels: np.ndarray | None = None) -> None:
        import torch

        torch.manual_seed(self.seed)
        x = self._sequences(train)
        if len(x) > self.max_rows:
            x = x[np.random.default_rng(self.seed).choice(len(x), self.max_rows, replace=False)]
        self.mean, self.std = x.mean(axis=0), x.std(axis=0) + 1e-6
        data = torch.tensor((x - self.mean) / self.std, dtype=torch.float32)
        dim = data.shape[1]
        encoder = torch.nn.Sequential(
            torch.nn.Linear(dim, 64), torch.nn.ReLU(), torch.nn.Linear(64, 16)
        )
        decoder = torch.nn.Sequential(
            torch.nn.Linear(8, 64), torch.nn.ReLU(), torch.nn.Linear(64, dim)
        )
        params = list(encoder.parameters()) + list(decoder.parameters())
        optimiser = torch.optim.Adam(params, lr=1e-3)
        for _ in range(self.epochs):
            for batch in torch.split(data[torch.randperm(len(data))], 512):
                stats = encoder(batch)
                mu, logvar = stats[:, :8], stats[:, 8:]
                z = mu + torch.randn_like(mu) * torch.exp(0.5 * logvar)
                recon = decoder(z)
                kl = -0.5 * torch.mean(1 + logvar - mu.pow(2) - logvar.exp())
                loss = torch.nn.functional.mse_loss(recon, batch) + 0.01 * kl
                optimiser.zero_grad()
                loss.backward()  # type: ignore[no-untyped-call]
                optimiser.step()
        self.model = (encoder, decoder)

    def score(self, test: pl.DataFrame) -> np.ndarray:
        import torch

        assert self.model is not None and self.mean is not None and self.std is not None
        encoder, decoder = self.model
        data = torch.tensor((self._sequences(test) - self.mean) / self.std, dtype=torch.float32)
        with torch.no_grad():
            recon = decoder(encoder(data)[:, :8])
        return np.asarray(((recon - data) ** 2).mean(dim=1).numpy(), dtype=float)


def e2d_columns(ablation: str = "none", window: str | None = None) -> tuple[str, ...]:
    """E2D's feature set, less what an ablation removes."""
    behaviour = BEHAVIOUR_FEATURES
    if window is not None:
        behaviour = tuple(
            f
            for f in BEHAVIOUR_FEATURES
            if not any(f.endswith(f"_{w}") for w in WINDOWS) or f.endswith(f"_{window}")
        )
    groups = {
        "behaviour": behaviour,
        "explanation": EXPLANATION_FEATURES,
        "forgery": FORGERY_FEATURES,
        "graph": GRAPH_FEATURES,
    }
    if ablation == "no_forgery":
        groups.pop("forgery")
    elif ablation == "no_graph":
        groups.pop("graph")
    columns = tuple(c for group in groups.values() for c in group)
    if ablation == "role_onehot":
        columns = (*columns, *ROLE_FEATURES)
    return columns


@dataclass
class E2D:
    """Gate, then one scorer per clinic (global fallback), as in the product."""

    scorer: str = "iforest"
    seed: int = 0
    columns: tuple[str, ...] = field(default_factory=e2d_columns)
    per_role: bool = False
    # Per-role models trained on the same number of rows each (role-conditioning ablation).
    equal_size: bool = False
    train_share: float = 1.0
    min_rows: int = 500
    name: str = "e2d"
    feature_delay_hours: float = FORGERY_DELAY_HOURS
    models: dict[str, ModelBundle] = field(default_factory=dict)
    fallback: ModelBundle | None = None

    @property
    def key(self) -> str:
        return "role" if self.per_role else "clinic_id"

    def fit(self, train: pl.DataFrame, labels: np.ndarray | None = None) -> None:
        gated = train.filter(gate(train))
        if self.train_share < 1.0:
            gated = gated.sample(fraction=self.train_share, seed=self.seed)
        groups = {str(k[0]): v for k, v in gated.partition_by(self.key, as_dict=True).items()}
        if self.equal_size and groups:
            smallest = min(v.height for v in groups.values())
            groups = {k: v.sample(n=smallest, seed=self.seed) for k, v in groups.items()}
        self.models, self.fallback = fit_per_clinic(
            groups, self.columns, self.scorer, self.seed, self.min_rows
        )

    def score(self, test: pl.DataFrame) -> np.ndarray:
        out = np.zeros(test.height)
        gated = gate(test).to_numpy()
        keys = test[self.key].to_numpy()
        for key in np.unique(keys):
            rows = (keys == key) & gated
            if not rows.any():
                continue
            model = self.models.get(str(key), self.fallback)
            if model is None:
                continue
            out[rows] = model.score(test.filter(pl.Series(rows)))
        # Gated accesses always rank above ungated ones, which score 0.
        return np.where(gated, out - out[gated].min() + 1e-6 if gated.any() else out, 0.0)


@dataclass
class UpperBoundLGBM:
    seed: int = 0
    name: str = "upper_lightgbm"
    feature_delay_hours: float = FORGERY_DELAY_HOURS
    model: Any = None
    columns: tuple[str, ...] = field(
        default_factory=lambda: (*e2d_columns(), *CONTEXT_COLUMNS, *ROLE_FEATURES)
    )

    def fit(self, train: pl.DataFrame, labels: np.ndarray | None = None) -> None:
        import lightgbm

        if labels is None:
            raise ValueError("the upper bound needs training labels")
        self.model = lightgbm.LGBMClassifier(
            n_estimators=200, learning_rate=0.05, class_weight="balanced",
            random_state=self.seed, verbose=-1,
        )  # fmt: skip
        self.model.fit(_x(train, self.columns), labels)

    def score(self, test: pl.DataFrame) -> np.ndarray:
        return np.asarray(self.model.predict_proba(_x(test, self.columns))[:, 1])


def make_method(name: str, seed: int, ablation: str = "none", **options: Any) -> Method:
    if name == "b0_rules":
        return RulesB0()
    if name == "b1_iforest":
        return IForest(per_role=False, seed=seed, name=name)
    if name == "b2_iforest_role":
        return IForest(per_role=True, seed=seed, name=name)
    if name == "b3_vae":
        return SequenceVAE(seed=seed, epochs=int(options.get("vae_epochs", 3)))
    if name == "b4_coaccess":
        return CoAccessB4()
    if name == "b5_explanation_only":
        return ExplanationOnlyB5()
    if name.startswith("e2d"):
        scorer = name.split("_", 1)[1] if "_" in name else "iforest"
        per_role = ablation == "role_per_role" or ablation.startswith("learning_curve")
        return E2D(
            scorer=scorer,
            seed=seed,
            columns=e2d_columns(ablation, options.get("window")),
            per_role=per_role,
            equal_size=per_role,
            train_share=float(options.get("train_share", 1.0)),
            name=name,
        )
    if name == "upper_lightgbm":
        return UpperBoundLGBM(seed=seed)
    raise ValueError(f"unknown method {name!r}")
