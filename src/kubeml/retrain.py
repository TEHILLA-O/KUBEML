"""Drift-triggered retraining job.

The job compares a new labelled batch against the champion's training data, checks
whether the champion has lost accuracy on that batch, and retrains only when the
policy says so. A retrained model is logged to MLflow and registered as a new
version with the ``candidate`` alias. It is never promoted to champion here:
promotion still goes through the release flow and its gates.

    kubeml-retrain --scenario drifted --rows 2000 --batch-seed 101 --report reports/retrain.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from kubeml.adapter import CreditRiskModel
from kubeml.config import Settings, get_settings
from kubeml.dataset import fingerprint_frame, generate_frame, load_or_create
from kubeml.drift import analyse_frame
from kubeml.gates import run_gates
from kubeml.schema import FEATURE_NAMES, TARGET, default_policy
from kubeml.training import log_mlflow, train_credit_risk

RETRAIN_POLICY = {
    # Retrain when at least this many features breach the drift thresholds ...
    "min_drifted_features": 2,
    # ... or when champion ROC AUC on the new labelled batch falls this far below
    # its own validation ROC AUC.
    "max_auc_drop": 0.03,
    # Share of the new batch used as labelled training data. The rest is held out
    # so champion and candidate are compared on data neither was trained on.
    "window_share": 0.5,
}


def _xy(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    frame = frame.reset_index(drop=True)
    return frame[FEATURE_NAMES], frame[TARGET]


def _headline(metrics: dict[str, Any]) -> dict[str, Any]:
    keep = ("samples", "accuracy", "precision", "recall", "f1", "roc_auc", "brier")
    return {key: metrics[key] for key in keep}


def decide(
    drift: dict[str, Any], champion_auc_ref: float, champion_auc_now: float | None, rules: dict
) -> dict[str, Any]:
    reasons = []
    drifted = drift["drifted_features"]
    if drift["sufficient_evidence"] and len(drifted) >= int(rules["min_drifted_features"]):
        reasons.append(f"{len(drifted)} features drifted: {', '.join(drifted)}")
    auc_drop = None
    if champion_auc_now is not None:
        auc_drop = champion_auc_ref - champion_auc_now
        if auc_drop > float(rules["max_auc_drop"]):
            reasons.append(f"champion ROC AUC fell by {auc_drop:.3f}")
    return {"retrain": bool(reasons), "reasons": reasons, "champion_auc_drop": auc_drop}


def _drift_metrics(report: dict[str, Any]) -> dict[str, float]:
    out = {}
    for feature, values in report["drift"]["per_feature"].items():
        for key in ("psi", "ks_statistic", "js_divergence"):
            if isinstance(values.get(key), float):
                out[f"drift_{feature}_{key}"] = values[key]
    out["drifted_feature_count"] = float(len(report["drift"]["drifted_features"]))
    if report["decision"]["champion_auc_drop"] is not None:
        out["champion_auc_drop"] = float(report["decision"]["champion_auc_drop"])
    return out


def _log_check(settings: Settings, report: dict[str, Any]) -> dict[str, Any]:
    """Log a drift check that did not trigger retraining, so every check is tracked."""
    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment("kubeml-retraining")
    with mlflow.start_run(run_name=f"check-{report['batch']['scenario']}") as run:
        mlflow.log_params(
            {
                "scenario": report["batch"]["scenario"],
                "batch_rows": report["batch"]["rows"],
                "batch_seed": report["batch"]["seed"],
                **{f"policy_{k}": v for k, v in RETRAIN_POLICY.items()},
            }
        )
        mlflow.set_tag("outcome", report["outcome"])
        mlflow.set_tag("batch_fingerprint", report["batch"]["fingerprint"])
        mlflow.log_metrics(_drift_metrics(report))
        mlflow.log_metrics(
            {
                f"champion_holdout_{k}": float(v)
                for k, v in report["champion"]["holdout"].items()
                if isinstance(v, float)
            }
        )
        mlflow.log_dict(report, "retraining_report.json")
        return {"run_id": run.info.run_id, "registry_version": None, "alias": None}


def _log_mlflow(settings: Settings, report: dict[str, Any], artifact: Path) -> dict[str, Any]:
    import mlflow
    from mlflow.tracking import MlflowClient

    from kubeml.training import _CreditRiskPyfunc

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment("kubeml-retraining")
    with mlflow.start_run(run_name=f"retrain-{report['batch']['scenario']}") as run:
        mlflow.log_params(
            {
                "scenario": report["batch"]["scenario"],
                "batch_rows": report["batch"]["rows"],
                "batch_seed": report["batch"]["seed"],
                "train_rows": report["candidate"]["train_rows"],
                **{f"policy_{k}": v for k, v in RETRAIN_POLICY.items()},
            }
        )
        mlflow.set_tag("trigger_reasons", "; ".join(report["decision"]["reasons"]))
        mlflow.set_tag("outcome", report["outcome"])
        mlflow.log_metrics(_drift_metrics(report))
        mlflow.set_tag("artifact_sha256", report["candidate"]["artifact_sha256"])
        mlflow.set_tag("batch_fingerprint", report["batch"]["fingerprint"])
        for prefix, metrics in (
            ("candidate_holdout", report["candidate"]["holdout"]),
            ("champion_holdout", report["champion"]["holdout"]),
            ("candidate_reference_test", report["candidate"]["reference_test"]),
        ):
            mlflow.log_metrics(
                {f"{prefix}_{k}": float(v) for k, v in metrics.items() if isinstance(v, float)}
            )
        mlflow.log_dict(report, "retraining_report.json")
        info = mlflow.pyfunc.log_model(
            artifact_path="model",
            python_model=_CreditRiskPyfunc(),
            artifacts={"model_json": str(artifact)},
        )
        run_id = run.info.run_id
    registered = mlflow.register_model(info.model_uri, settings.model_name)
    alias = "candidate" if report["gates_passed"] else None
    if alias:
        MlflowClient(tracking_uri=settings.mlflow_tracking_uri).set_registered_model_alias(
            settings.model_name, alias, registered.version
        )
    return {"run_id": run_id, "registry_version": str(registered.version), "alias": alias}


def _ensure_champion(settings: Settings, trained: dict[str, Any]) -> dict[str, Any]:
    """Register the current champion once, so retrained versions sit after it."""
    from mlflow.exceptions import MlflowException
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=settings.mlflow_tracking_uri)
    try:
        existing = client.get_model_version_by_alias(settings.model_name, "champion")
        return {"registry_version": str(existing.version), "alias": "champion", "new": False}
    except MlflowException:
        logged = log_mlflow(settings, trained, alias="champion")
        return {**logged, "new": True}


def run_retraining(
    settings: Settings,
    scenario: str,
    rows: int,
    batch_seed: int,
    log_to_mlflow: bool = True,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rules = {**RETRAIN_POLICY, **(rules or {})}
    policy = default_policy()
    split = load_or_create(
        settings.data_dir / "datasets" / "credit_risk_v1.csv",
        settings.dataset_rows,
        settings.dataset_seed,
    )
    trained = train_credit_risk(settings)
    champion = trained["model"]
    champion_validation = trained["metrics"]["validation"]
    champion_registry = _ensure_champion(settings, trained) if log_to_mlflow else None

    batch = generate_frame(rows, batch_seed, scenario)
    cut = int(len(batch) * float(rules["window_share"]))
    window, holdout = (
        batch.iloc[:cut].reset_index(drop=True),
        batch.iloc[cut:].reset_index(drop=True),
    )
    window_x, window_y = _xy(window)
    holdout_x, holdout_y = _xy(holdout)

    drift = analyse_frame(split.train[FEATURE_NAMES], window_x, policy)
    champion_window = champion.evaluate(
        window_x.fillna(window_x.median(numeric_only=True)), window_y
    )
    decision = decide(drift, champion_validation["roc_auc"], champion_window["roc_auc"], rules)

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "batch": {
            "scenario": scenario,
            "rows": rows,
            "seed": batch_seed,
            "window_rows": len(window),
            "holdout_rows": len(holdout),
            "fingerprint": fingerprint_frame(batch),
        },
        "policy": rules,
        "drift": {
            "result": drift["result"],
            "drifted_features": drift["drifted_features"],
            "per_feature": {
                item["feature"]: {
                    "result": item["result"],
                    **{
                        k: v
                        for k, v in item.get("metrics", {}).items()
                        if not k.endswith("threshold")
                    },
                    **({"psi": item["psi"]} if "psi" in item else {}),
                }
                for item in drift["features"]
            },
        },
        "champion": {
            "validation": _headline(champion_validation),
            "new_batch_window": _headline(champion_window),
        },
        "decision": decision,
    }
    if champion_registry:
        report["champion"]["registry"] = champion_registry
    if not decision["retrain"]:
        report["champion"]["holdout"] = _headline(champion.evaluate(holdout_x, holdout_y))
        report["outcome"] = "no_retrain"
        if log_to_mlflow:
            report["mlflow"] = _log_check(settings, report)
        return report

    train_frame = pd.concat([split.train, window], ignore_index=True).dropna()
    candidate = CreditRiskModel().fit(
        train_frame[FEATURE_NAMES], train_frame[TARGET], settings.dataset_seed
    )
    artifact_dir = settings.data_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / f"credit-risk-retrain-{scenario}-{batch_seed}.json"
    digest = candidate.save(artifact)

    holdout_eval = holdout.dropna().reset_index(drop=True)
    hx, hy = _xy(holdout_eval)
    cand_holdout = candidate.evaluate(hx, hy)
    champ_holdout = champion.evaluate(hx, hy)
    cand_ref_test = candidate.evaluate(split.test_x, split.test_y)
    gates = run_gates(
        train_frame,
        {"validation": cand_holdout, "test": cand_holdout},
        candidate,
        {"test": champ_holdout},
        policy,
        digest,
    )
    report["candidate"] = {
        "train_rows": int(len(train_frame)),
        "artifact_sha256": digest,
        "holdout": _headline(cand_holdout),
        "reference_test": _headline(cand_ref_test),
    }
    report["champion"]["holdout"] = _headline(champ_holdout)
    report["champion"]["reference_test"] = _headline(champion.evaluate(split.test_x, split.test_y))
    report["gates"] = [
        {"gate": g["gate"], "passed": g["passed"], "summary": g["summary"]} for g in gates
    ]
    report["gates_passed"] = all(g["passed"] for g in gates)
    report["outcome"] = "candidate_registered" if report["gates_passed"] else "candidate_rejected"
    if log_to_mlflow:
        report["mlflow"] = _log_mlflow(settings, report, artifact)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Check drift and retrain when the policy says so.")
    parser.add_argument(
        "--scenario",
        default="reference",
        choices=["reference", "drifted", "missing", "shift_purpose"],
    )
    parser.add_argument("--rows", type=int, default=2000)
    parser.add_argument("--batch-seed", type=int, default=101)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--no-mlflow", action="store_true")
    args = parser.parse_args()
    report = run_retraining(
        get_settings(), args.scenario, args.rows, args.batch_seed, log_to_mlflow=not args.no_mlflow
    )
    text = json.dumps(report, indent=2, default=str)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    print(
        json.dumps(
            {
                "outcome": report["outcome"],
                "decision": report["decision"],
                "mlflow": report.get("mlflow"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
