from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    mlflow_tracking_uri: str
    mlflow_required: bool
    jwt_secret: str
    token_ttl_seconds: int
    environment: str
    dataset_rows: int
    dataset_seed: int
    serving_stable_url: str
    serving_candidate_url: str
    alert_token: str
    model_name: str

    @property
    def sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def get_settings() -> Settings:
    data_dir = Path(os.environ.get("KUBEML_DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    default_db = f"sqlite:///{(data_dir / 'kubeml.sqlite3').as_posix()}"
    tracking = os.environ.get("MLFLOW_TRACKING_URI", (data_dir / "mlruns").as_uri())
    return Settings(
        data_dir=data_dir,
        database_url=os.environ.get("KUBEML_DATABASE_URL", default_db),
        mlflow_tracking_uri=tracking,
        mlflow_required=os.environ.get("KUBEML_MLFLOW_REQUIRED", "").lower()
        in {"1", "true", "yes"},
        jwt_secret=os.environ.get("KUBEML_JWT_SECRET", "local-dev-only-change-me"),
        token_ttl_seconds=int(os.environ.get("KUBEML_TOKEN_TTL_SECONDS", "43200")),
        environment=os.environ.get("KUBEML_ENVIRONMENT", "local"),
        dataset_rows=int(os.environ.get("KUBEML_DATASET_ROWS", "4000")),
        dataset_seed=int(os.environ.get("KUBEML_DATASET_SEED", "7")),
        serving_stable_url=os.environ.get("KUBEML_SERVING_STABLE_URL", "").rstrip("/"),
        serving_candidate_url=os.environ.get("KUBEML_SERVING_CANDIDATE_URL", "").rstrip("/"),
        alert_token=os.environ.get("KUBEML_ALERT_TOKEN", "local-alert-token"),
        model_name=os.environ.get("KUBEML_MODEL_NAME", "credit-risk"),
    )
