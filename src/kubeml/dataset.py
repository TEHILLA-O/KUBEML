from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from kubeml.schema import CATEGORICAL, FEATURE_NAMES, NUMERIC_NAMES, TARGET


@dataclass(frozen=True)
class DatasetSplit:
    frame: pd.DataFrame
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame
    fingerprint: str
    seed: int

    @property
    def train_x(self) -> pd.DataFrame:
        return self.train[FEATURE_NAMES]

    @property
    def train_y(self) -> pd.Series:
        return self.train[TARGET].astype(int)

    @property
    def validation_x(self) -> pd.DataFrame:
        return self.validation[FEATURE_NAMES]

    @property
    def validation_y(self) -> pd.Series:
        return self.validation[TARGET].astype(int)

    @property
    def test_x(self) -> pd.DataFrame:
        return self.test[FEATURE_NAMES]

    @property
    def test_y(self) -> pd.Series:
        return self.test[TARGET].astype(int)


def _sigmoid(logits: np.ndarray) -> np.ndarray:
    clipped = np.clip(logits, -30, 30)
    return 1.0 / (1.0 + np.exp(-clipped))


def generate_frame(rows: int, seed: int, scenario: str = "reference") -> pd.DataFrame:
    """Draw a deterministic credit book from a known logistic process.

    Scenarios change the input distribution. They are used by drift drills and never
    by the champion training split unless the caller asks for ``reference``.
    """
    rng = np.random.default_rng(seed)
    age = rng.integers(22, 74, rows)
    income = np.clip(rng.lognormal(mean=10.8, sigma=0.35, size=rows), 12_000, 280_000)
    debt = np.clip(rng.beta(2.2, 5.5, rows), 0.01, 1.2)
    history = np.clip(rng.normal(9, 5, rows), 0, 40)
    accounts = rng.integers(1, 14, rows)
    delinquencies = rng.choice([0, 1, 2, 3, 4, 6], size=rows, p=[0.62, 0.18, 0.1, 0.05, 0.03, 0.02])
    employment = np.clip(rng.normal(6, 4, rows), 0, 40)
    loan = np.clip(rng.lognormal(mean=9.1, sigma=0.45, size=rows), 800, 60_000)
    housing = rng.choice(["rent", "own", "mortgage"], size=rows, p=[0.4, 0.22, 0.38])
    purpose = rng.choice(
        ["debt", "home", "auto", "education", "medical"],
        size=rows,
        p=[0.42, 0.14, 0.22, 0.1, 0.12],
    )

    if scenario == "drifted":
        debt = np.clip(debt + 0.35, 0.01, 1.45)
        housing = rng.choice(["rent", "own", "mortgage"], size=rows, p=[0.78, 0.07, 0.15])
        income = income * 0.72
        delinquencies = np.clip(delinquencies + rng.integers(0, 3, rows), 0, 12)
    elif scenario == "missing":
        debt = debt.astype(float)
        debt[::7] = np.nan
    elif scenario == "shift_purpose":
        purpose = rng.choice(
            ["debt", "home", "auto", "education", "medical"],
            size=rows,
            p=[0.08, 0.08, 0.08, 0.08, 0.68],
        )

    housing_effect = np.array(
        [{"rent": 0.45, "mortgage": 0.12, "own": -0.28}[value] for value in housing]
    )
    purpose_effect = np.array(
        [
            {"debt": 0.38, "medical": 0.28, "auto": 0.02, "education": -0.12, "home": -0.22}[value]
            for value in purpose
        ]
    )
    logits = (
        -2.6
        + 1.45 * delinquencies
        + 3.8 * np.nan_to_num(debt, nan=0.4)
        - 0.000022 * income
        + 0.00005 * loan
        - 0.12 * history
        - 0.08 * employment
        + 0.16 * accounts
        - 0.02 * age
        + 1.5 * housing_effect
        + 1.5 * purpose_effect
    )
    probabilities = _sigmoid(logits)
    outcomes = rng.binomial(1, np.clip(probabilities, 1e-4, 1 - 1e-4))
    frame = pd.DataFrame(
        {
            "age": age.astype(int),
            "annual_income": np.round(income, 2),
            "debt_ratio": np.round(debt, 4),
            "credit_history_years": np.round(history, 2),
            "open_accounts": accounts.astype(int),
            "delinquencies_2y": delinquencies.astype(int),
            "employment_years": np.round(employment, 2),
            "loan_amount": np.round(loan, 2),
            "housing": housing,
            "loan_purpose": purpose,
            TARGET: outcomes.astype(int),
        }
    )
    return frame


def fingerprint_frame(frame: pd.DataFrame) -> str:
    payload = frame.to_csv(index=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def split_dataset(frame: pd.DataFrame, seed: int) -> DatasetSplit:
    """Split once, in index order, after a seeded shuffle.

    The test partition is reserved for baseline comparison. Training fits only the
    training partition. Thresholds are policy constants, not values searched on test.
    """
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(frame))
    shuffled = frame.iloc[order].reset_index(drop=True)
    n = len(shuffled)
    train_end = int(n * 0.7)
    valid_end = int(n * 0.85)
    digest = fingerprint_frame(frame)
    return DatasetSplit(
        frame=frame.reset_index(drop=True),
        train=shuffled.iloc[:train_end].reset_index(drop=True),
        validation=shuffled.iloc[train_end:valid_end].reset_index(drop=True),
        test=shuffled.iloc[valid_end:].reset_index(drop=True),
        fingerprint=digest,
        seed=seed,
    )


def load_or_create(path, rows: int, seed: int) -> DatasetSplit:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        frame = pd.read_csv(path)
    else:
        frame = generate_frame(rows, seed, "reference")
        frame.to_csv(path, index=False)
    return split_dataset(frame, seed)


def sample_requests(count: int, seed: int, scenario: str) -> list[dict]:
    frame = generate_frame(count, seed, scenario if scenario != "invalid" else "reference")
    records = []
    for row in frame.to_dict(orient="records"):
        outcome = int(row.pop(TARGET))
        features = {}
        for name in FEATURE_NAMES:
            value = row[name]
            if hasattr(value, "item"):
                value = value.item()
            if isinstance(value, float) and value != value:
                value = None
            features[name] = value
        if scenario == "invalid" and len(records) % 2 == 0:
            features["debt_ratio"] = None
            features["housing"] = "houseboat"
        records.append({"features": features, "outcome": outcome})
    return records


def assert_known_categories() -> None:
    for name, values in CATEGORICAL.items():
        if len(set(values)) != len(values):
            raise RuntimeError(f"duplicate categories for {name}")
    if set(NUMERIC_NAMES).intersection(CATEGORICAL):
        raise RuntimeError("feature name collision")
