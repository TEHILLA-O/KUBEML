from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import Response

from kubeml.adapter import InputValidationError
from kubeml.config import get_settings
from kubeml.engine import load_engine, read_faults
from kubeml.metrics import (
    INFERENCE_BATCH,
    INFERENCE_DURATION,
    INFERENCE_ERRORS,
    INFERENCE_REQUESTS,
    LAST_SUCCESS,
    MODEL_LOADED,
)
from kubeml.schema import MAX_BATCH, MAX_PAYLOAD_BYTES

_ENGINE = None
_LOADED_AT = None


class PredictRequest(BaseModel):
    features: dict = Field(default_factory=dict)


class BatchRequest(BaseModel):
    instances: list[dict] = Field(default_factory=list)


def _engine():
    if _ENGINE is None:
        raise RuntimeError("model is not loaded")
    return _ENGINE


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _ENGINE, _LOADED_AT
        path = os.environ.get("KUBEML_MODEL_PATH", "")
        if not path:
            _ENGINE = None
            MODEL_LOADED.labels(service="serving", slot="stable").set(0)
        else:
            version = int(os.environ.get("KUBEML_MODEL_VERSION", "0"))
            identifier = os.environ.get("KUBEML_MODEL_VERSION_ID", "baked")
            _ENGINE = load_engine(path, identifier, version)
            _LOADED_AT = time.time()
            MODEL_LOADED.labels(
                service="serving", slot=os.environ.get("KUBEML_SLOT", "stable")
            ).set(1)
        yield

    app = FastAPI(title="KubeML Serving", version="0.1.0", lifespan=lifespan)

    @app.get("/health/live")
    def live() -> dict:
        faults = read_faults(settings.data_dir)
        if faults.get("not_live"):
            return JSONResponse({"status": "down"}, status_code=500)
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> dict:
        faults = read_faults(settings.data_dir)
        if _ENGINE is None or faults.get("not_ready"):
            return JSONResponse(
                {"status": "not_ready", "model_loaded": _ENGINE is not None}, status_code=503
            )
        return {
            "status": "ready",
            "model_version_id": _ENGINE.model_version_id,
            "model_version": _ENGINE.version,
        }

    @app.get("/model/info")
    def info() -> dict:
        if _ENGINE is None:
            return JSONResponse({"loaded": False}, status_code=503)
        payload = _ENGINE.info()
        payload["loaded"] = True
        payload["loaded_at"] = _LOADED_AT
        payload["note"] = (
            "This process loaded the artifact path given at startup. It does not follow MLflow alias changes."
        )
        return payload

    @app.post("/predict")
    def predict(body: PredictRequest, request: Request) -> dict:
        _reject_large(request)
        return _predict_many([body.features], "predict")[0]

    @app.post("/predict/batch")
    def predict_batch(body: BatchRequest, request: Request) -> dict:
        _reject_large(request)
        if len(body.instances) < 1:
            return JSONResponse(
                {"errors": [{"field": "instances", "message": "Batch is empty."}]}, status_code=422
            )
        if len(body.instances) > MAX_BATCH:
            return JSONResponse(
                {"errors": [{"field": "instances", "message": f"Batch exceeds {MAX_BATCH}."}]},
                status_code=422,
            )
        predictions = _predict_many(body.instances, "batch")
        return {"predictions": predictions, "count": len(predictions)}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def _reject_large(request: Request) -> None:
    length = request.headers.get("content-length")
    if length and int(length) > MAX_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"errors": [{"field": "$", "message": "Payload exceeds the configured limit."}]},
        )


def _predict_many(instances: list[dict], endpoint: str) -> list[dict]:
    slot = os.environ.get("KUBEML_SLOT", "stable")
    environment = os.environ.get("KUBEML_ENVIRONMENT", "local")
    started = time.perf_counter()
    try:
        if _ENGINE is None:
            raise RuntimeError("model_not_loaded")
        predictions = [
            _ENGINE.predict_one(features, get_settings().data_dir) for features in instances
        ]
    except InputValidationError as exc:
        INFERENCE_REQUESTS.labels(
            service="serving", environment=environment, endpoint=endpoint, status="error", slot=slot
        ).inc()
        INFERENCE_ERRORS.labels(
            service="serving",
            environment=environment,
            endpoint=endpoint,
            error_type="validation",
            slot=slot,
        ).inc()
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc
    except Exception as exc:  # noqa: BLE001
        INFERENCE_REQUESTS.labels(
            service="serving", environment=environment, endpoint=endpoint, status="error", slot=slot
        ).inc()
        INFERENCE_ERRORS.labels(
            service="serving",
            environment=environment,
            endpoint=endpoint,
            error_type=exc.__class__.__name__,
            slot=slot,
        ).inc()
        raise HTTPException(status_code=500, detail={"error": str(exc)}) from exc
    duration = time.perf_counter() - started
    INFERENCE_REQUESTS.labels(
        service="serving", environment=environment, endpoint=endpoint, status="ok", slot=slot
    ).inc()
    INFERENCE_DURATION.labels(
        service="serving", environment=environment, endpoint=endpoint, slot=slot
    ).observe(duration)
    INFERENCE_BATCH.labels(service="serving", environment=environment).observe(len(instances))
    LAST_SUCCESS.labels(service="serving", slot=slot).set(time.time())
    return predictions


def main() -> None:
    import uvicorn

    uvicorn.run(
        "kubeml.serving.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8090")),
    )


if __name__ == "__main__":
    main()
