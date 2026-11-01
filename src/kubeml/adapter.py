from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from kubeml.schema import (
    CATEGORICAL,
    CATEGORICAL_NAMES,
    FEATURE_NAMES,
    NEGATIVE_LABEL,
    NUMERIC_NAMES,
    POSITIVE_LABEL,
    RANGES,
    SCHEMA_VERSION,
    TASK,
)


class InputValidationError(ValueError):
    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(json.dumps(errors))


class ModelAdapter(ABC):
    """Family-specific model boundary.

    Classification, regression, and language models keep their own evaluation methods.
    Callers must not assume a shared metric set beyond this contract.
    """

    @abstractmethod
    def load_model(self, path: str | Path) -> None: ...

    @abstractmethod
    def validate_input(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def preprocess(self, frame: pd.DataFrame) -> np.ndarray: ...

    @abstractmethod
    def predict(self, features: pd.DataFrame | dict[str, Any]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def postprocess(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def evaluate(self, features: pd.DataFrame, labels: pd.Series) -> dict[str, Any]: ...

    @abstractmethod
    def get_model_metadata(self) -> dict[str, Any]: ...


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def validate_features(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InputValidationError(
            [{"field": "$", "message": "Feature payload must be an object."}]
        )
    errors: list[dict[str, str]] = []
    cleaned: dict[str, Any] = {}
    unknown = sorted(set(payload) - set(FEATURE_NAMES))
    for name in unknown:
        errors.append({"field": name, "message": "Unknown feature."})
    for name in FEATURE_NAMES:
        if name not in payload or payload[name] is None:
            errors.append({"field": name, "message": "Required feature is missing."})
            continue
        value = payload[name]
        if name in RANGES:
            if not _is_number(value):
                errors.append({"field": name, "message": "Expected a finite number."})
                continue
            number = float(value)
            low, high = RANGES[name]
            if number < low or number > high:
                errors.append({"field": name, "message": f"Value is outside [{low}, {high}]."})
                continue
            cleaned[name] = number
        else:
            if not isinstance(value, str):
                errors.append({"field": name, "message": "Expected a category string."})
                continue
            allowed = CATEGORICAL[name]
            if value not in allowed:
                errors.append(
                    {"field": name, "message": f"Category must be one of {', '.join(allowed)}."}
                )
                continue
            cleaned[name] = value
    if errors:
        raise InputValidationError(errors)
    return cleaned


def frame_quality(frame: pd.DataFrame) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    missing_columns = [name for name in FEATURE_NAMES if name not in frame.columns]
    extra = [name for name in frame.columns if name not in FEATURE_NAMES and name != "defaulted"]
    if missing_columns:
        issues.append({"check": "required_features", "detail": missing_columns})
    if extra:
        issues.append({"check": "unexpected_columns", "detail": extra})
    present = frame.copy()
    missing_rates = {}
    out_of_range = {}
    invalid_categories = {}
    for name in NUMERIC_NAMES:
        if name not in present.columns:
            continue
        series = pd.to_numeric(present[name], errors="coerce")
        missing_rates[name] = float(series.isna().mean())
        low, high = RANGES[name]
        comparable = series.dropna()
        out_of_range[name] = (
            float(((comparable < low) | (comparable > high)).mean()) if len(comparable) else 0.0
        )
        if missing_rates[name] > 0:
            issues.append({"check": "missing_values", "field": name, "rate": missing_rates[name]})
        if out_of_range[name] > 0:
            issues.append({"check": "value_range", "field": name, "rate": out_of_range[name]})
    for name, allowed in CATEGORICAL.items():
        if name not in present.columns:
            continue
        series = present[name]
        missing_rates[name] = float(series.isna().mean())
        observed = series.dropna().astype(str)
        bad = ~observed.isin(allowed)
        invalid_categories[name] = float(bad.mean()) if len(observed) else 0.0
        if missing_rates[name] > 0:
            issues.append({"check": "missing_values", "field": name, "rate": missing_rates[name]})
        if invalid_categories[name] > 0:
            issues.append(
                {"check": "invalid_category", "field": name, "rate": invalid_categories[name]}
            )
    duplicate_rate = 0.0
    if len(frame):
        duplicate_rate = float(frame.duplicated().mean())
        if duplicate_rate > 0.01:
            issues.append({"check": "duplicate_records", "rate": duplicate_rate})
    return {
        "rows": int(len(frame)),
        "missing_rates": missing_rates,
        "out_of_range_rates": out_of_range,
        "invalid_category_rates": invalid_categories,
        "duplicate_rate": duplicate_rate,
        "issues": issues,
        "passed": not issues,
    }


class CreditRiskModel(ModelAdapter):
    def __init__(self) -> None:
        self.artifact: dict[str, Any] | None = None
        self.path: Path | None = None
        self.sha256: str | None = None

    def fit(self, features: pd.DataFrame, labels: pd.Series, seed: int) -> "CreditRiskModel":
        scaler = StandardScaler()
        encoder = OneHotEncoder(
            categories=[list(CATEGORICAL[name]) for name in CATEGORICAL_NAMES],
            handle_unknown="ignore",
            sparse_output=False,
        )
        scaled = scaler.fit_transform(features[NUMERIC_NAMES])
        encoded = encoder.fit_transform(features[CATEGORICAL_NAMES])
        matrix = np.hstack([scaled, encoded])
        classifier = LogisticRegression(max_iter=400, random_state=seed, solver="lbfgs")
        classifier.fit(matrix, labels.astype(int))
        names = list(NUMERIC_NAMES)
        for column, categories in zip(CATEGORICAL_NAMES, encoder.categories_, strict=True):
            names.extend(f"{column}={category}" for category in categories)
        self.artifact = {
            "format": "kubeml.logistic.json.v1",
            "task": TASK,
            "schema_version": SCHEMA_VERSION,
            "framework": f"scikit-learn logistic coefficients ({classifier.__class__.__name__})",
            "estimator": "logistic_regression",
            "positive_label": POSITIVE_LABEL,
            "negative_label": NEGATIVE_LABEL,
            "threshold": 0.5,
            "seed": seed,
            "numeric": NUMERIC_NAMES,
            "categorical": {name: list(CATEGORICAL[name]) for name in CATEGORICAL_NAMES},
            "means": scaler.mean_.tolist(),
            "scales": [value if value else 1.0 for value in scaler.scale_.tolist()],
            "feature_order": names,
            "coefficients": classifier.coef_.ravel().tolist(),
            "intercept": float(classifier.intercept_[0]),
        }
        return self

    def save(self, path: str | Path) -> str:
        if self.artifact is None:
            raise RuntimeError("Model has not been fit or loaded.")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(self.artifact, indent=2, sort_keys=True).encode("utf-8")
        target.write_bytes(encoded)
        self.path = target
        self.sha256 = hashlib.sha256(encoded).hexdigest()
        return self.sha256

    def load_model(self, path: str | Path) -> None:
        target = Path(path)
        encoded = target.read_bytes()
        payload = json.loads(encoded)
        if payload.get("format") != "kubeml.logistic.json.v1":
            raise ValueError(
                "Refusing to load an artifact that is not a KubeML logistic JSON model."
            )
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Feature schema version does not match this serving build.")
        coefficients = payload["coefficients"]
        if len(coefficients) != len(payload["feature_order"]):
            raise ValueError("Coefficient vector does not match the feature order.")
        self.artifact = payload
        self.path = target
        self.sha256 = hashlib.sha256(encoded).hexdigest()

    def validate_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        return validate_features(payload)

    def preprocess(self, frame: pd.DataFrame) -> np.ndarray:
        if self.artifact is None:
            raise RuntimeError("Model is not loaded.")
        means = np.asarray(self.artifact["means"], dtype=float)
        scales = np.asarray(self.artifact["scales"], dtype=float)
        numeric = frame[NUMERIC_NAMES].astype(float).to_numpy()
        scaled = (numeric - means) / scales
        encoded_columns = []
        for name in CATEGORICAL_NAMES:
            allowed = self.artifact["categorical"][name]
            values = frame[name].astype(str).to_numpy()
            for category in allowed:
                encoded_columns.append((values == category).astype(float))
        encoded = np.column_stack(encoded_columns) if encoded_columns else np.zeros((len(frame), 0))
        return np.hstack([scaled, encoded])

    def _raw_predict(self, matrix: np.ndarray) -> list[dict[str, Any]]:
        coefficients = np.asarray(self.artifact["coefficients"], dtype=float)
        intercept = float(self.artifact["intercept"])
        logits = np.clip(matrix @ coefficients + intercept, -30, 30)
        scores = 1.0 / (1.0 + np.exp(-logits))
        threshold = float(self.artifact["threshold"])
        results = []
        for score in scores:
            label = POSITIVE_LABEL if score >= threshold else NEGATIVE_LABEL
            results.append(
                {
                    "label": label,
                    "score": float(score),
                    "positive_label": POSITIVE_LABEL,
                    "threshold": threshold,
                }
            )
        return results

    def predict(self, features: pd.DataFrame | dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(features, dict):
            frame = pd.DataFrame([self.validate_input(features)])
        else:
            records = [self.validate_input(row) for row in features.to_dict(orient="records")]
            frame = pd.DataFrame(records)
        return self.postprocess(self._raw_predict(self.preprocess(frame)))

    def postprocess(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        processed = []
        for item in raw:
            score = item["score"]
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise RuntimeError("Model produced a non-finite or out-of-range score.")
            processed.append(
                {
                    "label": item["label"],
                    "score": score,
                    "positive_label": item["positive_label"],
                    "threshold": item["threshold"],
                    "task": TASK,
                }
            )
        return processed

    def evaluate(self, features: pd.DataFrame, labels: pd.Series) -> dict[str, Any]:
        predictions = self.predict(features)
        scores = np.asarray([item["score"] for item in predictions], dtype=float)
        predicted = np.asarray(
            [1 if item["label"] == POSITIVE_LABEL else 0 for item in predictions], dtype=int
        )
        actual = labels.astype(int).to_numpy()
        matrix = confusion_matrix(actual, predicted, labels=[0, 1]).tolist()
        metrics = {
            "samples": int(len(actual)),
            "accuracy": float(accuracy_score(actual, predicted)),
            "precision": float(precision_score(actual, predicted, zero_division=0)),
            "recall": float(recall_score(actual, predicted, zero_division=0)),
            "f1": float(f1_score(actual, predicted, zero_division=0)),
            "roc_auc": float(roc_auc_score(actual, scores)) if len(set(actual)) > 1 else None,
            "brier": float(brier_score_loss(actual, scores)),
            "positive_rate": float(predicted.mean()),
            "confusion_matrix": {
                "labels": [NEGATIVE_LABEL, POSITIVE_LABEL],
                "matrix": matrix,
            },
        }
        segments: dict[str, Any] = {}
        for name in CATEGORICAL_NAMES:
            for category, index in features.groupby(name).groups.items():
                segment_actual = actual[list(index)]
                segment_scores = scores[list(index)]
                segment_predicted = predicted[list(index)]
                if len(segment_actual) < 20 or len(set(segment_actual)) < 2:
                    segments[f"{name}={category}"] = {
                        "samples": int(len(segment_actual)),
                        "roc_auc": None,
                    }
                    continue
                segments[f"{name}={category}"] = {
                    "samples": int(len(segment_actual)),
                    "roc_auc": float(roc_auc_score(segment_actual, segment_scores)),
                    "f1": float(f1_score(segment_actual, segment_predicted, zero_division=0)),
                }
        metrics["segments"] = segments
        return metrics

    def get_model_metadata(self) -> dict[str, Any]:
        if self.artifact is None:
            raise RuntimeError("Model is not loaded.")
        return {
            "name": "credit-risk",
            "task": TASK,
            "framework": "scikit-learn",
            "estimator": self.artifact["estimator"],
            "format": self.artifact["format"],
            "schema_version": self.artifact["schema_version"],
            "feature_order": self.artifact["feature_order"],
            "threshold": self.artifact["threshold"],
            "sha256": self.sha256,
            "path": str(self.path) if self.path else None,
            "positive_label": POSITIVE_LABEL,
            "negative_label": NEGATIVE_LABEL,
            "coefficient_count": len(self.artifact["coefficients"]),
        }


def score_separation(model: CreditRiskModel) -> dict[str, float]:
    from kubeml.schema import SMOKE_FIXTURES

    scored = {item["name"]: model.predict(item["features"])[0]["score"] for item in SMOKE_FIXTURES}
    return scored
