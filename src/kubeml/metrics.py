from prometheus_client import Counter, Gauge, Histogram

INFERENCE_REQUESTS = Counter(
    "kubeml_inference_requests_total",
    "Inference requests handled by a KubeML process.",
    ["service", "environment", "endpoint", "status", "slot"],
)
INFERENCE_ERRORS = Counter(
    "kubeml_inference_errors_total",
    "Inference requests that failed or were rejected.",
    ["service", "environment", "endpoint", "error_type", "slot"],
)
INFERENCE_DURATION = Histogram(
    "kubeml_inference_duration_seconds",
    "Inference latency in seconds.",
    ["service", "environment", "endpoint", "slot"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
INFERENCE_BATCH = Histogram(
    "kubeml_inference_batch_size",
    "Accepted batch sizes.",
    ["service", "environment"],
    buckets=(1, 2, 4, 8, 16, 32, 64),
)
MODEL_LOADED = Gauge(
    "kubeml_model_loaded",
    "Whether a pinned model slot is loaded.",
    ["service", "slot"],
)
LAST_SUCCESS = Gauge(
    "kubeml_model_last_successful_inference_timestamp_seconds",
    "Unix time of the last successful inference for a slot.",
    ["service", "slot"],
)
TRAINING_JOBS = Counter(
    "kubeml_training_jobs_total",
    "Training jobs by terminal or queued status.",
    ["status", "runtime"],
)
TRAINING_DURATION = Histogram(
    "kubeml_training_duration_seconds",
    "Training job duration.",
    ["status"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)
DEPLOYMENTS = Counter(
    "kubeml_deployments_total",
    "Release deployment attempts.",
    ["environment", "result"],
)
ROLLBACKS = Counter(
    "kubeml_rollbacks_total",
    "Rollback attempts.",
    ["environment", "result"],
)
GATE_FAILURES = Counter(
    "kubeml_release_gate_failures_total",
    "Quality gate failures.",
    ["gate"],
)
