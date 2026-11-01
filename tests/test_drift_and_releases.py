import numpy as np
import pandas as pd

from kubeml.drift import analyse_frame, population_stability_index
from kubeml.releases import (
    TransitionError,
    assert_transition,
    evaluate_canary,
    rollback_authorised,
    summarise_routes,
)
from kubeml.schema import FEATURE_NAMES, default_policy


def test_psi_is_near_zero_for_identical_samples():
    values = np.linspace(0, 1, 200)
    assert population_stability_index(values, values) < 0.02


def test_shifted_distribution_increases_psi():
    reference = np.random.default_rng(1).normal(0, 1, 400)
    shifted = reference + 2.5
    assert population_stability_index(reference, shifted) > population_stability_index(
        reference, reference
    )


def test_drift_report_flags_shifted_debt_without_authorising_rollback():
    reference = pd.DataFrame({name: [0] * 80 for name in FEATURE_NAMES})
    reference["age"] = np.linspace(25, 60, 80)
    reference["annual_income"] = np.linspace(30000, 90000, 80)
    reference["debt_ratio"] = np.linspace(0.1, 0.4, 80)
    reference["credit_history_years"] = 8
    reference["open_accounts"] = 4
    reference["delinquencies_2y"] = 0
    reference["employment_years"] = 5
    reference["loan_amount"] = 8000
    reference["housing"] = "own"
    reference["loan_purpose"] = "auto"
    current = reference.copy()
    current["debt_ratio"] = np.linspace(0.9, 1.3, 80)
    current["housing"] = "rent"
    report = analyse_frame(reference, current, default_policy())
    assert "debt_ratio" in report["drifted_features"]
    decision = rollback_authorised(
        "feature_drift", {"requests": 100}, default_policy(), manual=False
    )
    assert decision["authorised"] is False


def test_canary_requires_enough_requests():
    stats = summarise_routes(
        [
            {"slot": "candidate", "latency_ms": 12, "status": "ok", "error_type": ""}
            for _ in range(3)
        ]
    )
    stats["stable"] = {
        "requests": 3,
        "errors": 0,
        "error_rate": 0,
        "invalid_outputs": 0,
        "p50_ms": 10,
        "p95_ms": 12,
        "p99_ms": 12,
    }
    decision = evaluate_canary(stats, default_policy(), True, True)
    assert decision["decision"] == "insufficient_evidence"


def test_illegal_transition_is_rejected():
    try:
        assert_transition("candidate", "promoted")
    except TransitionError:
        return
    raise AssertionError("candidate was allowed to skip to promoted")
