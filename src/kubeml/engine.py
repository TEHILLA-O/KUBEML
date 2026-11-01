from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from kubeml.adapter import CreditRiskModel, InputValidationError

_FAULT_LOCK = threading.Lock()


def fault_path(data_dir: Path) -> Path:
    return data_dir / "faults.json"


def read_faults(data_dir: Path) -> dict[str, Any]:
    path = fault_path(data_dir)
    if not path.exists():
        return {
            "latency_ms": 0,
            "error_rate": 0.0,
            "invalid_output": False,
            "not_ready": False,
            "not_live": False,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "latency_ms": 0,
            "error_rate": 0.0,
            "invalid_output": False,
            "not_ready": False,
            "not_live": False,
        }


def write_faults(data_dir: Path, faults: dict[str, Any]) -> dict[str, Any]:
    current = read_faults(data_dir)
    current.update(faults)
    with _FAULT_LOCK:
        fault_path(data_dir).write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


class InferenceEngine:
    """One pinned model. Alias changes elsewhere do not reload this object."""

    def __init__(self, model: CreditRiskModel, model_version_id: str, version: int):
        self.model = model
        self.model_version_id = model_version_id
        self.version = version
        self.loaded_at = time.time()
        self._calls = 0
        self._lock = threading.Lock()

    def info(self) -> dict[str, Any]:
        metadata = self.model.get_model_metadata()
        metadata.update(
            {
                "model_version_id": self.model_version_id,
                "version": self.version,
                "loaded_at": self.loaded_at,
            }
        )
        return metadata

    def predict_one(self, features: dict[str, Any], data_dir: Path) -> dict[str, Any]:
        faults = read_faults(data_dir)
        started = time.perf_counter()
        with self._lock:
            self._calls += 1
            call = self._calls
        if faults.get("latency_ms"):
            time.sleep(min(float(faults["latency_ms"]), 5000) / 1000)
        if float(faults.get("error_rate") or 0) > 0 and (call % 100) < int(
            float(faults["error_rate"]) * 100
        ):
            raise RuntimeError("injected_error")
        try:
            prediction = self.model.predict(features)[0]
        except InputValidationError:
            raise
        if faults.get("invalid_output"):
            prediction = {**prediction, "score": float("nan"), "label": "unknown"}
            raise RuntimeError("invalid_output")
        latency_ms = (time.perf_counter() - started) * 1000
        return {
            "prediction_id": f"prd_{uuid.uuid4().hex[:16]}",
            "model_version_id": self.model_version_id,
            "model_version": self.version,
            "model_sha256": self.model.sha256,
            **prediction,
            "latency_ms": latency_ms,
        }


class TrafficRouter:
    def __init__(self) -> None:
        self.stable: InferenceEngine | None = None
        self.candidate: InferenceEngine | None = None
        self.weight = 0
        self.counter = 0
        self.lock = threading.Lock()
        self.runtime = "local-router"

    def choose(self) -> str:
        with self.lock:
            self.counter += 1
            if (
                self.candidate is not None
                and self.weight > 0
                and (self.counter % 100) < self.weight
            ):
                return "candidate"
            return "stable"

    def engine(self, slot: str) -> InferenceEngine:
        selected = self.candidate if slot == "candidate" else self.stable
        if selected is None:
            raise RuntimeError(f"{slot} model is not loaded")
        return selected

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "weight": self.weight,
            "observed_decisions": self.counter,
            "stable": None if self.stable is None else self.stable.info(),
            "candidate": None if self.candidate is None else self.candidate.info(),
        }


def load_engine(path: str, model_version_id: str, version: int) -> InferenceEngine:
    model = CreditRiskModel()
    model.load_model(path)
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if model.sha256 != digest:
        raise RuntimeError("Artifact digest changed while loading.")
    return InferenceEngine(model, model_version_id, version)
