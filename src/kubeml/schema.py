"""Feature contract for the credit-risk classifier.

The repository had no trained model, dataset, or training script. This schema is the
fallback demonstration model described in the model integration assessment.
"""

from __future__ import annotations

SCHEMA_VERSION = "credit-risk.features.v1"
MODEL_NAME = "credit-risk"
TASK = "binary_classification"
POSITIVE_LABEL = "default"
NEGATIVE_LABEL = "no_default"
TARGET = "defaulted"

NUMERIC = (
    ("age", 21, 80),
    ("annual_income", 8_000, 400_000),
    ("debt_ratio", 0.0, 1.5),
    ("credit_history_years", 0, 45),
    ("open_accounts", 0, 30),
    ("delinquencies_2y", 0, 12),
    ("employment_years", 0, 50),
    ("loan_amount", 500, 80_000),
)
CATEGORICAL = {
    "housing": ("rent", "own", "mortgage"),
    "loan_purpose": ("debt", "home", "auto", "education", "medical"),
}
NUMERIC_NAMES = [name for name, _, _ in NUMERIC]
CATEGORICAL_NAMES = list(CATEGORICAL)
FEATURE_NAMES = NUMERIC_NAMES + CATEGORICAL_NAMES
RANGES = {name: (low, high) for name, low, high in NUMERIC}

# Fixed profiles used by the inference smoke gate. Expectations are constraints, not a
# copied probability, so a legitimate retrain does not fail the gate on float drift.
SMOKE_FIXTURES = (
    {
        "name": "low_risk_profile",
        "features": {
            "age": 46,
            "annual_income": 128000,
            "debt_ratio": 0.12,
            "credit_history_years": 16,
            "open_accounts": 3,
            "delinquencies_2y": 0,
            "employment_years": 12,
            "loan_amount": 6000,
            "housing": "own",
            "loan_purpose": "home",
        },
    },
    {
        "name": "high_risk_profile",
        "features": {
            "age": 24,
            "annual_income": 24000,
            "debt_ratio": 0.92,
            "credit_history_years": 1,
            "open_accounts": 12,
            "delinquencies_2y": 5,
            "employment_years": 0,
            "loan_amount": 28000,
            "housing": "rent",
            "loan_purpose": "debt",
        },
    },
    {
        "name": "mid_risk_profile",
        "features": {
            "age": 35,
            "annual_income": 62000,
            "debt_ratio": 0.38,
            "credit_history_years": 7,
            "open_accounts": 5,
            "delinquencies_2y": 1,
            "employment_years": 4,
            "loan_amount": 12000,
            "housing": "mortgage",
            "loan_purpose": "auto",
        },
    },
)

MAX_BATCH = 64
MAX_PAYLOAD_BYTES = 1_000_000


def feature_contract() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "model_name": MODEL_NAME,
        "task": TASK,
        "target": TARGET,
        "positive_label": POSITIVE_LABEL,
        "negative_label": NEGATIVE_LABEL,
        "features": [
            {
                "name": name,
                "dtype": "number",
                "minimum": RANGES[name][0],
                "maximum": RANGES[name][1],
            }
            for name in NUMERIC_NAMES
        ]
        + [
            {"name": name, "dtype": "category", "allowed": list(CATEGORICAL[name])}
            for name in CATEGORICAL_NAMES
        ],
    }


def default_policy() -> dict:
    return {
        "evaluation": {
            "min_roc_auc": 0.70,
            "min_f1": 0.45,
            "min_accuracy": 0.65,
            "max_auc_regression": 0.03,
            "max_f1_regression": 0.05,
            "min_samples": 150,
        },
        "canary": {
            "steps": [5, 20, 50, 100],
            "min_requests": 30,
            "max_error_rate": 0.05,
            "max_p95_latency_seconds": 0.75,
            "max_latency_regression_ratio": 2.5,
        },
        "rollback": {
            "deployment_timeout_seconds": 180,
            "on_readiness_failure": True,
            "on_error_rate": True,
            "on_latency_regression": True,
            "on_invalid_output": True,
            "on_smoke_failure": True,
            "on_rollout_timeout": True,
            "on_crash_loop": True,
            "feature_drift_auto_rollback": False,
        },
        "drift": {
            "psi_threshold": 0.2,
            "ks_pvalue_threshold": 0.01,
            "js_threshold": 0.12,
            "categorical_psi_threshold": 0.2,
            "missing_rate_delta": 0.08,
            "out_of_range_delta": 0.05,
            "min_samples": 40,
        },
        "labels": {"min_coverage": 0.2},
        "serving": {"max_batch": MAX_BATCH, "decision_threshold": 0.5},
    }
