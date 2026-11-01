# KubeML

KubeML is a local-first model operations platform. It trains a deterministic credit-risk classifier, records the run, gates the artifact, serves predictions from a pinned model, and controls canary traffic and rollback with evidence.

## Model integration assessment

The KubeML workspace was empty. There was no training script, saved model, dataset, feature definition, or evaluation test to adapt. The platform therefore uses a fallback model:

| | |
| --- | --- |
| Task | Binary classification of synthetic loan default |
| Estimator | L2 logistic regression, CPU, scikit-learn coefficients stored as JSON |
| Inputs | 8 numeric features and 2 categorical features (`credit-risk.features.v1`) |
| Output | `default` or `no_default`, plus a probability in `[0, 1]` |
| Data | Deterministic synthetic book written to `data/datasets/credit_risk_v1.csv` |
| Split | 70% train, 15% validation, 15% fixed test. The scaler is fit on train only. |
| Baseline | Validation ROC AUC is gated at 0.70. The test split is only used for champion comparison. |
| Size | A few kilobytes of JSON. No GPU. |
| Inference | In-process local router, or the FastAPI serving image |

Serving loads that JSON file. It does not unpickle arbitrary artifacts and it does not follow an MLflow alias change inside a running process.

## What is actually running

- **Local router (default).** `kubeml-platform` trains on startup if no champion exists, loads the pinned artifact, and routes canary traffic in-process. This is a real router with a deterministic weight counter. It is not a Kubernetes deployment.
- **Serving process.** `kubeml-serving` loads `KUBEML_MODEL_PATH` once at startup.
- **MLflow.** Runs are logged to the tracking URI (file store by default, PostgreSQL and MinIO in Compose). If tracking fails and `KUBEML_MLFLOW_REQUIRED` is not set, the local registry still records the run and the UI shows the tracking error.
- **Kubernetes.** The cluster view calls `kubectl`. If there is no cluster, the page says so. Staging deploy is refused without a reachable cluster and an image digest.
- **Prometheus and Grafana.** Compose and the Helm chart scrape the real metrics. The mission-control charts read the operational database of gateway requests and label that source.
- **Cloud.** `terraform/aws` is an optional EKS path. Nothing in CI runs `terraform apply`.

## Local demo accounts

These passwords exist only for the local database seed.

| Username | Role | Password |
| --- | --- | --- |
| admin | Platform administrator | `kubeml-local` |
| engineer | ML engineer | `kubeml-local` |
| approver | Release approver | `kubeml-local` |
| observer | Read-only observer | `kubeml-local` |

Change `KUBEML_JWT_SECRET` before exposing the API beyond your machine. The default secret is for local development.

## Run without Docker

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\kubeml-platform
```

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. The API listens on `http://127.0.0.1:8080`.

Optional inference container process, after the platform has written an artifact:

```powershell
$env:KUBEML_MODEL_PATH = (Get-ChildItem data\artifacts\*.json | Select-Object -First 1).FullName
.\.venv\Scripts\kubeml-serving
```

## Run with Docker Compose

Compose adds PostgreSQL, MinIO, MLflow, Prometheus, Alertmanager, and Grafana. It does not pretend those processes exist when you use the SQLite path above.

```powershell
docker compose up --build
```

The platform API is published on port 8096 so it does not take over a process already using 8080. Grafana is on port 3000 (`admin` / `kubeml-local`). Prometheus is on port 9090. MLflow is on port 5000. Alertmanager is on port 9093.

After the serving image builds, record its local image id when the baked artifact matches a registered model:

```powershell
.\.venv\Scripts\python scripts\record_image.py
```

## Kubernetes

```bash
bash scripts/kind/up.sh
```

The script creates a kind cluster, installs metrics-server, builds the images, and installs the Helm chart. HPA is only meaningful after metrics-server is Available. The chart's production values require image digests. `latest` is not a production identifier.

`scripts/kind/down.sh` deletes the cluster.

## Tests

```powershell
.\.venv\Scripts\python -m pytest
cd frontend
npm run typecheck
npm test
```

Playwright covers the signed-in mission flow when the API and the Vite server are already running:

```powershell
cd frontend
npm run test:e2e
```

## Release rules that are enforced in the API

- Training returns a job id before the run finishes.
- A candidate is not promoted because training succeeded. Quality gates must pass.
- Canary weight only increases after the configured sample size and error, latency, and validity checks. Too few requests stay `insufficient_evidence`.
- Feature drift opens an investigation. It cannot be saved as an automatic rollback trigger.
- Rollback is idempotent, shifts traffic back to the pinned stable model, smokes that model, and leaves the incident unresolved if the smoke test fails.
- Staging deploy does not report success without a connected cluster and an immutable image digest.
