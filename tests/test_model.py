import math

import pandas as pd

from kubeml.adapter import CreditRiskModel, InputValidationError, validate_features
from kubeml.config import Settings
from kubeml.dataset import generate_frame, split_dataset
from kubeml.gates import run_gates
from kubeml.schema import SMOKE_FIXTURES, default_policy
from kubeml.training import train_credit_risk


def test_high_risk_scores_above_low_risk(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 't.sqlite3').as_posix()}",
        mlflow_tracking_uri=(tmp_path / "mlruns").as_uri(),
        mlflow_required=False,
        jwt_secret="test",
        token_ttl_seconds=60,
        environment="local",
        dataset_rows=1600,
        dataset_seed=7,
        serving_stable_url="",
        serving_candidate_url="",
        alert_token="t",
        model_name="credit-risk",
    )
    trained = train_credit_risk(settings)
    model: CreditRiskModel = trained["model"]
    scores = {item["name"]: model.predict(item["features"])[0]["score"] for item in SMOKE_FIXTURES}
    assert scores["high_risk_profile"] > scores["low_risk_profile"]
    assert trained["metrics"]["validation"]["roc_auc"] > 0.70
    assert all(math.isfinite(score) for score in scores.values())


def test_rejects_unknown_category_and_missing_feature():
    payload = dict(SMOKE_FIXTURES[0]["features"])
    payload["housing"] = "houseboat"
    try:
        validate_features(payload)
    except InputValidationError as exc:
        assert any(item["field"] == "housing" for item in exc.errors)
    else:
        raise AssertionError("invalid category was accepted")
    incomplete = dict(SMOKE_FIXTURES[0]["features"])
    del incomplete["debt_ratio"]
    try:
        validate_features(incomplete)
    except InputValidationError as exc:
        assert any(item["field"] == "debt_ratio" for item in exc.errors)
    else:
        raise AssertionError("missing feature was accepted")


def test_preprocessor_is_fit_on_training_rows_only(tmp_path):
    settings = _settings(tmp_path)
    trained = train_credit_risk(settings)
    model: CreditRiskModel = trained["model"]
    split = split_dataset(
        pd.read_csv(tmp_path / "datasets" / "credit_risk_v1.csv"), settings.dataset_seed
    )
    train_mean_income = float(split.train["annual_income"].mean())
    assert abs(model.artifact["means"][1] - train_mean_income) < 1e-6
    assert abs(model.artifact["means"][1] - float(split.test["annual_income"].mean())) > 1


def test_gates_pass_for_reference_model(tmp_path):
    settings = _settings(tmp_path)
    trained = train_credit_risk(settings)
    outcomes = run_gates(
        trained["full_frame"],
        trained["metrics"],
        trained["model"],
        None,
        default_policy(),
        trained["artifact_sha256"],
    )
    failed = [item["gate"] for item in outcomes if not item["passed"]]
    assert failed == []


def test_loaded_json_matches_in_memory_scores(tmp_path):
    settings = _settings(tmp_path)
    trained = train_credit_risk(settings)
    loaded = CreditRiskModel()
    loaded.load_model(trained["artifact_path"])
    features = SMOKE_FIXTURES[2]["features"]
    assert (
        abs(trained["model"].predict(features)[0]["score"] - loaded.predict(features)[0]["score"])
        < 1e-9
    )


def test_refuses_non_json_artifact(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"format": "pickle"}', encoding="utf-8")
    model = CreditRiskModel()
    try:
        model.load_model(path)
    except ValueError as exc:
        assert "Refusing" in str(exc)
    else:
        raise AssertionError("untrusted artifact was loaded")


def _settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        database_url="sqlite://",
        mlflow_tracking_uri=(tmp_path / "mlruns").as_uri(),
        mlflow_required=False,
        jwt_secret="test",
        token_ttl_seconds=60,
        environment="local",
        dataset_rows=1600,
        dataset_seed=7,
        serving_stable_url="",
        serving_candidate_url="",
        alert_token="t",
        model_name="credit-risk",
    )


def test_split_partitions_are_disjoint():
    frame = generate_frame(300, 7)
    split = split_dataset(frame, 7)
    train_ids = set(map(tuple, split.train.to_numpy()))
    test_ids = set(map(tuple, split.test.to_numpy()))
    assert train_ids.isdisjoint(test_ids)
