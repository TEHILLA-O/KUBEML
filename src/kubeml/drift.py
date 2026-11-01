"""Distribution checks used by the drift observatory.

Population stability index, Kolmogorov-Smirnov, and Jensen-Shannon divergence answer
different questions. A small p-value is not treated as a release decision by itself.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

from kubeml.schema import CATEGORICAL, NUMERIC_NAMES


def _epsilon_normalise(counts: np.ndarray) -> np.ndarray:
    adjusted = counts.astype(float) + 1e-6
    return adjusted / adjusted.sum()


def _histogram(
    reference: np.ndarray, current: np.ndarray, bins: int
) -> tuple[np.ndarray, np.ndarray]:
    quantiles = np.linspace(0, 1, bins + 1)
    edges = np.unique(np.quantile(reference, quantiles))
    if len(edges) < 3:
        edges = np.linspace(float(np.min(reference)), float(np.max(reference)) + 1e-9, num=3)
    reference_counts, _ = np.histogram(reference, bins=edges)
    current_counts, _ = np.histogram(current, bins=edges)
    return _epsilon_normalise(reference_counts), _epsilon_normalise(current_counts)


def population_stability_index(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    expected, actual = _histogram(
        np.asarray(reference, dtype=float), np.asarray(current, dtype=float), bins
    )
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def js_divergence(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    expected, actual = _histogram(
        np.asarray(reference, dtype=float), np.asarray(current, dtype=float), bins
    )
    mixture = 0.5 * (expected + actual)
    return float(
        0.5 * np.sum(expected * np.log(expected / mixture))
        + 0.5 * np.sum(actual * np.log(actual / mixture))
    )


def categorical_psi(reference: pd.Series, current: pd.Series, categories: list[str]) -> float:
    def distribution(series: pd.Series) -> np.ndarray:
        counts = np.array(
            [(series.astype(str) == category).sum() for category in categories], dtype=float
        )
        return _epsilon_normalise(counts)

    expected = distribution(reference)
    actual = distribution(current)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def categorical_shares(series: pd.Series, categories: list[str]) -> dict[str, float]:
    if len(series) == 0:
        return {category: 0.0 for category in categories}
    return {category: float((series.astype(str) == category).mean()) for category in categories}


def numeric_histogram(
    values: np.ndarray, edges: list[float] | None = None, bins: int = 12
) -> dict[str, Any]:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if len(array) == 0:
        return {"edges": [], "counts": []}
    counts, bin_edges = np.histogram(array, bins=edges if edges is not None else bins)
    return {
        "edges": [float(edge) for edge in bin_edges],
        "counts": [int(count) for count in counts],
    }


def analyse_frame(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    policy: dict[str, Any],
) -> dict[str, Any]:
    rules = policy["drift"]
    min_samples = int(rules["min_samples"])
    results = []
    sufficient = len(reference) >= min_samples and len(current) >= min_samples
    for name in NUMERIC_NAMES:
        ref = pd.to_numeric(reference[name], errors="coerce")
        cur = pd.to_numeric(current[name], errors="coerce")
        ref_missing = float(ref.isna().mean()) if len(ref) else 0.0
        cur_missing = float(cur.isna().mean()) if len(cur) else 0.0
        ref_values = ref.dropna().to_numpy()
        cur_values = cur.dropna().to_numpy()
        entry: dict[str, Any] = {
            "feature": name,
            "kind": "numeric",
            "reference_samples": int(ref.notna().sum()),
            "current_samples": int(cur.notna().sum()),
            "missing_reference": ref_missing,
            "missing_current": cur_missing,
            "missing_delta": cur_missing - ref_missing,
        }
        if len(ref_values) < min_samples or len(cur_values) < min_samples:
            entry.update(
                {
                    "result": "insufficient_evidence",
                    "metrics": {},
                    "interpretation": "Not enough finite observations to compare this feature.",
                }
            )
            results.append(entry)
            continue
        psi = population_stability_index(ref_values, cur_values)
        statistic, pvalue = ks_2samp(ref_values, cur_values)
        divergence = js_divergence(ref_values, cur_values)
        low = float(np.min(ref_values))
        high = float(np.max(ref_values))
        current_oor = float(((cur_values < low) | (cur_values > high)).mean())
        breaches = []
        if psi >= float(rules["psi_threshold"]):
            breaches.append("psi")
        if pvalue < float(rules["ks_pvalue_threshold"]):
            breaches.append("ks")
        if divergence >= float(rules["js_threshold"]):
            breaches.append("js")
        if abs(cur_missing - ref_missing) >= float(rules["missing_rate_delta"]):
            breaches.append("missing_rate")
        if current_oor >= float(rules["out_of_range_delta"]):
            breaches.append("out_of_range")
        edges = numeric_histogram(ref_values)["edges"]
        entry.update(
            {
                "result": "drift" if breaches else "stable",
                "breaches": breaches,
                "metrics": {
                    "psi": psi,
                    "psi_threshold": float(rules["psi_threshold"]),
                    "ks_statistic": float(statistic),
                    "ks_pvalue": float(pvalue),
                    "ks_pvalue_threshold": float(rules["ks_pvalue_threshold"]),
                    "js_divergence": divergence,
                    "js_threshold": float(rules["js_threshold"]),
                    "out_of_range_rate": current_oor,
                },
                "reference_histogram": numeric_histogram(ref_values, edges=edges),
                "current_histogram": numeric_histogram(cur_values, edges=edges),
                "interpretation": _numeric_interpretation(name, breaches, psi, pvalue),
            }
        )
        results.append(entry)
    for name, categories in CATEGORICAL.items():
        ref = reference[name]
        cur = current[name]
        ref_missing = float(ref.isna().mean()) if len(ref) else 0.0
        cur_missing = float(cur.isna().mean()) if len(cur) else 0.0
        psi = categorical_psi(ref.dropna(), cur.dropna(), list(categories)) if sufficient else None
        breaches = []
        if psi is not None and psi >= float(rules["categorical_psi_threshold"]):
            breaches.append("categorical_psi")
        if abs(cur_missing - ref_missing) >= float(rules["missing_rate_delta"]):
            breaches.append("missing_rate")
        results.append(
            {
                "feature": name,
                "kind": "categorical",
                "reference_samples": int(ref.notna().sum()),
                "current_samples": int(cur.notna().sum()),
                "missing_reference": ref_missing,
                "missing_current": cur_missing,
                "missing_delta": cur_missing - ref_missing,
                "result": "insufficient_evidence"
                if not sufficient
                else ("drift" if breaches else "stable"),
                "breaches": breaches,
                "metrics": {
                    "psi": psi,
                    "psi_threshold": float(rules["categorical_psi_threshold"]),
                },
                "reference_share": categorical_shares(ref.dropna(), list(categories)),
                "current_share": categorical_shares(cur.dropna(), list(categories)),
                "interpretation": (
                    "Sample size is below the policy minimum."
                    if not sufficient
                    else (
                        "Category mix moved beyond the configured PSI threshold."
                        if breaches
                        else "Category mix is within the configured PSI threshold."
                    )
                ),
            }
        )
    drifted = [item["feature"] for item in results if item["result"] == "drift"]
    return {
        "sufficient_evidence": sufficient,
        "reference_rows": int(len(reference)),
        "current_rows": int(len(current)),
        "drifted_features": drifted,
        "result": "insufficient_evidence" if not sufficient else ("drift" if drifted else "stable"),
        "features": results,
        "note": (
            "Statistical thresholds are monitoring signals. Feature drift does not by itself "
            "authorise an automated model rollback."
        ),
    }


def _numeric_interpretation(name: str, breaches: list[str], psi: float, pvalue: float) -> str:
    if not breaches:
        return f"{name} is inside the configured numeric drift thresholds."
    parts = [f"{name} exceeded: {', '.join(breaches)}."]
    parts.append(f"PSI {psi:.3f}. KS p-value {pvalue:.4f}.")
    parts.append("This compares distributions. It does not measure prediction accuracy.")
    return " ".join(parts)


def finite(value: float) -> bool:
    return math.isfinite(value)
