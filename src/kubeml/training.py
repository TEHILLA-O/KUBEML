from __future__ import annotations

import json
import platform
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

import mlflow.pyfunc

from kubeml.adapter import CreditRiskModel
from kubeml.config import Settings
from kubeml.dataset import load_or_create
from kubeml.schema import SCHEMA_VERSION


def dependency_fingerprint() -> dict[str, str]:
    names = ["scikit-learn", "numpy", "pandas", "scipy", "mlflow"]
    found = {}
    for name in names:
        try:
            found[name] = version(name)
        except Exception:  # noqa: BLE001
            found[name] = "unavailable"
    found["python"] = sys.version.split()[0]
    found["platform"] = platform.platform()
    return found


def source_commit(root: Path) -> str:
    current = root.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            if candidate != root.resolve() and root.resolve() != candidate:
                # A parent repository is not this project's history.
                if candidate == root.resolve():
                    break
                if root.resolve() != candidate and (root / ".git").exists() is False:
                    return "unavailable"
            try:
                return subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=candidate if (root / ".git").exists() else candidate,
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except Exception:  # noqa: BLE001
                return "unavailable"
    return "unavailable"


def project_commit() -> str:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return "unavailable"
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001
        return "uncommitted"


def train_credit_risk(settings: Settings, seed: int | None = None) -> dict[str, Any]:
    seed_value = settings.dataset_seed if seed is None else seed
    dataset_path = settings.data_dir / "datasets" / "credit_risk_v1.csv"
    split = load_or_create(dataset_path, settings.dataset_rows, settings.dataset_seed)
    reference_path = settings.data_dir / "reference" / "credit_risk_train.csv"
    reference_path.parent.mkdir(parents=True, exist_ok=True)
    split.train.to_csv(reference_path, index=False)
    split.test.to_csv(settings.data_dir / "reference" / "credit_risk_test.csv", index=False)
    model = CreditRiskModel().fit(split.train_x, split.train_y, seed_value)
    validation = model.evaluate(split.validation_x, split.validation_y)
    test = model.evaluate(split.test_x, split.test_y)
    artifact_dir = settings.data_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    temporary = artifact_dir / f"credit-risk-seed{seed_value}.json"
    digest = model.save(temporary)
    metadata = model.get_model_metadata()
    return {
        "model": model,
        "artifact_path": str(temporary),
        "artifact_sha256": digest,
        "artifact_bytes": temporary.stat().st_size,
        "dataset_path": str(dataset_path),
        "dataset_fingerprint": split.fingerprint,
        "dataset_rows": int(len(split.frame)),
        "schema_version": SCHEMA_VERSION,
        "seed": seed_value,
        "split": {
            "train": int(len(split.train)),
            "validation": int(len(split.validation)),
            "test": int(len(split.test)),
        },
        "metrics": {"validation": validation, "test": test},
        "params": {
            "estimator": "logistic_regression",
            "seed": seed_value,
            "threshold": 0.5,
            "solver": "lbfgs",
        },
        "dependencies": dependency_fingerprint(),
        "git_commit": project_commit(),
        "metadata": metadata,
        "train_frame": split.train,
        "full_frame": split.frame,
    }


class _CreditRiskPyfunc(mlflow.pyfunc.PythonModel):
    def load_context(self, context) -> None:
        model = CreditRiskModel()
        model.load_model(context.artifacts["model_json"])
        self._model = model

    def predict(self, context, model_input, params=None):  # noqa: ANN001
        del context, params
        import pandas as pd

        if hasattr(model_input, "to_dict"):
            records = model_input.to_dict(orient="records")
        else:
            records = list(model_input)
        frame = pd.DataFrame(records)
        return [item["score"] for item in self._model.predict(frame)]


def log_mlflow(settings: Settings, trained: dict[str, Any], alias: str | None) -> dict[str, Any]:
    import mlflow
    from mlflow.tracking import MlflowClient

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment("kubeml-credit-risk")
    with mlflow.start_run(run_name=f"credit-risk-{trained['artifact_sha256'][:8]}") as run:
        mlflow.log_params({key: str(value) for key, value in trained["params"].items()})
        mlflow.set_tag("dataset_fingerprint", trained["dataset_fingerprint"])
        mlflow.set_tag("schema_version", trained["schema_version"])
        mlflow.set_tag("git_commit", trained["git_commit"])
        mlflow.set_tag("artifact_sha256", trained["artifact_sha256"])
        flat = {}
        for split_name, metrics in trained["metrics"].items():
            for key, value in metrics.items():
                if isinstance(value, (int, float)):
                    flat[f"{split_name}_{key}"] = float(value)
        mlflow.log_metrics(flat)
        mlflow.log_dict(
            {"metrics": trained["metrics"], "metadata": trained["metadata"]}, "evaluation.json"
        )
        info = mlflow.pyfunc.log_model(
            artifact_path="model",
            python_model=_CreditRiskPyfunc(),
            artifacts={"model_json": trained["artifact_path"]},
        )
        run_id = run.info.run_id
    client = MlflowClient(tracking_uri=settings.mlflow_tracking_uri)
    registered = mlflow.register_model(info.model_uri, settings.model_name)
    if alias:
        client.set_registered_model_alias(settings.model_name, alias, registered.version)
    return {"run_id": run_id, "registry_version": str(registered.version), "alias": alias}


def main() -> None:
    settings = __import__("kubeml.config", fromlist=["get_settings"]).get_settings()
    trained = train_credit_risk(settings)
    print(
        json.dumps(
            {"sha256": trained["artifact_sha256"], "metrics": trained["metrics"]["validation"]},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
