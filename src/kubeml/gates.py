from __future__ import annotations

from typing import Any

from kubeml.adapter import CreditRiskModel, frame_quality
from kubeml.schema import SMOKE_FIXTURES


def _outcome(name: str, passed: bool, evidence: dict[str, Any], summary: str) -> dict[str, Any]:
    return {"gate": name, "passed": passed, "summary": summary, "evidence": evidence}


def gate_data_quality(frame) -> dict[str, Any]:
    report = frame_quality(frame)
    return _outcome(
        "data_quality",
        report["passed"],
        report,
        "Dataset matches the feature contract."
        if report["passed"]
        else "Dataset failed schema or quality checks.",
    )


def gate_evaluation(metrics: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    rules = policy["evaluation"]
    validation = metrics["validation"]
    failures = []
    if validation["samples"] < int(rules["min_samples"]):
        failures.append("min_samples")
    if validation.get("roc_auc") is None or validation["roc_auc"] < float(rules["min_roc_auc"]):
        failures.append("min_roc_auc")
    if validation["f1"] < float(rules["min_f1"]):
        failures.append("min_f1")
    if validation["accuracy"] < float(rules["min_accuracy"]):
        failures.append("min_accuracy")
    return _outcome(
        "evaluation",
        not failures,
        {"failures": failures, "validation": validation, "thresholds": rules},
        "Validation metrics meet absolute thresholds."
        if not failures
        else "Validation metrics missed a threshold.",
    )


def gate_baseline(
    metrics: dict[str, Any], champion: dict[str, Any] | None, policy: dict[str, Any]
) -> dict[str, Any]:
    rules = policy["evaluation"]
    candidate = metrics["test"]
    if champion is None:
        return _outcome(
            "baseline",
            True,
            {"comparison": "no_champion", "candidate_test": candidate},
            "No champion is registered. Absolute thresholds are the only performance gate.",
        )
    baseline = champion["test"]
    failures = []
    if candidate["samples"] < int(rules["min_samples"]):
        failures.append("min_samples")
    auc_drop = None
    if baseline.get("roc_auc") is not None and candidate.get("roc_auc") is not None:
        auc_drop = float(baseline["roc_auc"]) - float(candidate["roc_auc"])
        if auc_drop > float(rules["max_auc_regression"]):
            failures.append("max_auc_regression")
    f1_drop = float(baseline["f1"]) - float(candidate["f1"])
    if f1_drop > float(rules["max_f1_regression"]):
        failures.append("max_f1_regression")
    return _outcome(
        "baseline",
        not failures,
        {
            "failures": failures,
            "auc_drop": auc_drop,
            "f1_drop": f1_drop,
            "candidate_test": candidate,
            "champion_test": baseline,
            "thresholds": {
                "max_auc_regression": rules["max_auc_regression"],
                "max_f1_regression": rules["max_f1_regression"],
            },
        },
        "Candidate stayed within the allowed regression against the champion test set."
        if not failures
        else "Candidate regressed against the champion on the fixed test set.",
    )


def gate_integrity(model: CreditRiskModel, expected_sha: str | None = None) -> dict[str, Any]:
    metadata = model.get_model_metadata()
    failures = []
    if not metadata.get("sha256"):
        failures.append("artifact_hash")
    if expected_sha and metadata["sha256"] != expected_sha:
        failures.append("digest_mismatch")
    if metadata["schema_version"] is None:
        failures.append("schema")
    if metadata["coefficient_count"] < 1:
        failures.append("coefficients")
    return _outcome(
        "integrity",
        not failures,
        {"failures": failures, "metadata": metadata},
        "Artifact loaded and the feature schema matches this build."
        if not failures
        else "Artifact integrity check failed.",
    )


def gate_smoke(model: CreditRiskModel) -> dict[str, Any]:
    cases = []
    failures = []
    for fixture in SMOKE_FIXTURES:
        try:
            prediction = model.predict(fixture["features"])[0]
            finite = 0.0 <= prediction["score"] <= 1.0
            label_ok = prediction["label"] in {prediction["positive_label"], "no_default"}
            passed = finite and label_ok and prediction["task"] == "binary_classification"
            if not passed:
                failures.append(fixture["name"])
            cases.append({"name": fixture["name"], "passed": passed, "prediction": prediction})
        except Exception as exc:  # noqa: BLE001 - smoke evidence must capture the failure
            failures.append(fixture["name"])
            cases.append({"name": fixture["name"], "passed": False, "error": str(exc)})
    low = next(item for item in cases if item["name"] == "low_risk_profile")
    high = next(item for item in cases if item["name"] == "high_risk_profile")
    ordered = True
    if low.get("prediction") and high.get("prediction"):
        ordered = high["prediction"]["score"] > low["prediction"]["score"]
        if not ordered:
            failures.append("risk_ordering")
    return _outcome(
        "smoke",
        not failures,
        {"failures": failures, "cases": cases, "high_score_exceeds_low_score": ordered},
        "Known profiles scored with finite probabilities and the expected risk ordering."
        if not failures
        else "Inference smoke checks failed.",
    )


def run_gates(
    frame,
    metrics: dict[str, Any],
    model: CreditRiskModel,
    champion: dict[str, Any] | None,
    policy: dict[str, Any],
    expected_sha: str | None = None,
) -> list[dict[str, Any]]:
    return [
        gate_data_quality(frame),
        gate_evaluation(metrics, policy),
        gate_baseline(metrics, champion, policy),
        gate_integrity(model, expected_sha),
        gate_smoke(model),
    ]
