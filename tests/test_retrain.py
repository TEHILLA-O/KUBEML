from kubeml.config import get_settings
from kubeml.retrain import RETRAIN_POLICY, decide, run_retraining


def _drift(features, sufficient=True):
    return {"sufficient_evidence": sufficient, "drifted_features": features}


def test_decide_needs_enough_drifted_features():
    assert decide(_drift(["debt_ratio"]), 0.9, 0.9, RETRAIN_POLICY)["retrain"] is False
    out = decide(_drift(["debt_ratio", "housing"]), 0.9, 0.9, RETRAIN_POLICY)
    assert out["retrain"] is True
    assert "2 features drifted" in out["reasons"][0]


def test_decide_retrains_on_performance_drop_without_drift():
    out = decide(_drift([]), 0.90, 0.80, RETRAIN_POLICY)
    assert out["retrain"] is True
    assert "ROC AUC fell" in out["reasons"][0]


def test_decide_ignores_drift_without_enough_evidence():
    out = decide(_drift(["a", "b", "c"], sufficient=False), 0.9, 0.9, RETRAIN_POLICY)
    assert out["retrain"] is False


def test_stable_batch_does_not_retrain(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBEML_DATA_DIR", str(tmp_path))
    report = run_retraining(get_settings(), "reference", 1000, 101, log_to_mlflow=False)
    assert report["outcome"] == "no_retrain"
    assert "candidate" not in report


def test_drifted_batch_retrains_and_registers_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBEML_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MLFLOW_TRACKING_URI", (tmp_path / "mlruns").as_uri())
    report = run_retraining(get_settings(), "drifted", 1000, 101, log_to_mlflow=True)
    assert report["decision"]["retrain"] is True
    assert {"debt_ratio", "housing"} <= set(report["drift"]["drifted_features"])
    assert report["gates_passed"] is True
    assert report["champion"]["registry"]["alias"] == "champion"
    assert report["mlflow"]["alias"] == "candidate"
    assert int(report["mlflow"]["registry_version"]) > int(
        report["champion"]["registry"]["registry_version"]
    )
