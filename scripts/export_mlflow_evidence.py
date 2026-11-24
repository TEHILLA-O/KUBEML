"""Export MLflow runs and registry versions to CSV/JSON so tracking is visible in the repo.

KUBEML_DATA_DIR=/tmp/kdata .venv/bin/python scripts/export_mlflow_evidence.py --out docs/evidence/mlflow
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import mlflow
from mlflow.tracking import MlflowClient

from kubeml.config import get_settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("docs/evidence/mlflow"))
    args = ap.parse_args()
    settings = get_settings()
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    client = MlflowClient(tracking_uri=settings.mlflow_tracking_uri)
    args.out.mkdir(parents=True, exist_ok=True)
    runs = mlflow.search_runs(
        experiment_names=["kubeml-credit-risk", "kubeml-retraining"], order_by=["start_time ASC"]
    )
    keep = [c for c in runs.columns if c.startswith(("params.", "metrics.", "tags."))]
    keep = [c for c in keep if not c.startswith("tags.mlflow.")]
    exp_names = {e.experiment_id: e.name for e in client.search_experiments()}
    runs["experiment"] = runs["experiment_id"].map(exp_names)
    runs[["run_id", "experiment", "status", *sorted(keep)]].to_csv(
        args.out / "runs.csv", index=False
    )
    versions = []
    for v in sorted(
        client.search_model_versions(f"name='{settings.model_name}'"), key=lambda v: int(v.version)
    ):
        run = client.get_run(v.run_id)
        versions.append(
            {
                "model": v.name,
                "version": int(v.version),
                "aliases": list(v.aliases),
                "run_id": v.run_id,
                "experiment": exp_names.get(run.info.experiment_id),
                "artifact_sha256": run.data.tags.get("artifact_sha256"),
                "dataset_or_batch_fingerprint": run.data.tags.get("dataset_fingerprint")
                or run.data.tags.get("batch_fingerprint"),
            }
        )
    (args.out / "registry_versions.json").write_text(json.dumps(versions, indent=2) + "\n")
    print(runs[["run_id", "experiment"]].to_string(index=False))
    print(json.dumps(versions, indent=1))


if __name__ == "__main__":
    main()
