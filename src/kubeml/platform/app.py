from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import Response

from kubeml.auth import allowed, issue_token, read_token, verify_password
from kubeml.config import get_settings
from kubeml.engine import read_faults
from kubeml.ops import Platform, PlatformError
from kubeml.releases import TransitionError
from kubeml.schema import feature_contract
from kubeml.store import (
    Alert,
    AuditEvent,
    DriftRun,
    GateResult,
    Incident,
    ModelVersion,
    PolicyDocument,
    Release,
    TrainingRun,
    User,
)


class LoginBody(BaseModel):
    username: str
    password: str


class TrainingBody(BaseModel):
    reason: str = "manual request"
    trigger: str = "manual"
    seed: int | None = None
    runtime: str = "local-thread"


class ReleaseBody(BaseModel):
    model_version_id: str
    environment: str = "local"


class ReasonBody(BaseModel):
    reason: str = ""


class CanaryBody(BaseModel):
    weight: int


class RollbackBody(BaseModel):
    reason: str
    trigger: str = "manual"


class TrafficBody(BaseModel):
    count: int = 25
    scenario: str = "reference"
    label_delay_seconds: int = 30


class LabelBody(BaseModel):
    min_age_seconds: int = 0


class PolicyBody(BaseModel):
    body: dict[str, Any]
    reason: str


class FaultBody(BaseModel):
    latency_ms: int | None = None
    error_rate: float | None = None
    invalid_output: bool | None = None
    not_ready: bool | None = None
    not_live: bool | None = None


class ImageBody(BaseModel):
    digest: str
    artifact_sha256: str
    repository: str = "kubeml-serving"


class PredictBody(BaseModel):
    features: dict[str, Any] = Field(default_factory=dict)


class IncidentBody(BaseModel):
    summary: str
    trigger: str = "investigation"
    model_version_id: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


def create_app(settings=None) -> FastAPI:
    settings = settings or get_settings()
    holder: dict[str, Platform] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["platform"] = Platform(settings)
        app.state.platform = holder["platform"]
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(20):
                try:
                    holder["platform"].scan()
                except Exception:
                    continue

        thread = None
        if os.environ.get("KUBEML_SCAN_LOOP", "1") != "0":
            thread = threading.Thread(target=loop, daemon=True)
            thread.start()
        yield
        stop.set()

    app = FastAPI(title="KubeML Platform", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(PlatformError)
    async def platform_error(_request: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(TransitionError)
    async def transition_error(_request: Request, exc: TransitionError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=409)

    def platform() -> Platform:
        return holder["platform"]

    def principal(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Authentication is required.")
        token = authorization.split(" ", 1)[1]
        service_token = os.environ.get("KUBEML_SERVICE_TOKEN", "")
        if service_token and token == service_token:
            return {"sub": "scheduler", "role": "engineer", "name": "Scheduler", "uid": "system"}
        try:
            return read_token(settings, token)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=401, detail="Token is invalid.") from exc

    def require(permission: str):
        def dependency(user: dict = Depends(principal)) -> dict:
            if not allowed(user["role"], permission):
                raise HTTPException(
                    status_code=403, detail=f"Role {user['role']} cannot {permission}."
                )
            return user

        return dependency

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"status": "alive", "service": "kubeml-platform"}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/api/v1/auth/login")
    def login(body: LoginBody, ops: Platform = Depends(platform)) -> dict:
        with ops.session() as session:
            user = session.scalar(select(User).where(User.username == body.username))
            if user is None or not verify_password(body.password, user.password_hash):
                raise HTTPException(status_code=401, detail="Unknown username or password.")
            return {
                "token": issue_token(settings, user),
                "user": {"username": user.username, "name": user.name, "role": user.role},
            }

    @app.get("/api/v1/me")
    def me(user: dict = Depends(principal)) -> dict:
        return user

    @app.get("/api/v1/contract")
    def contract() -> dict:
        return feature_contract()

    @app.get("/api/v1/summary")
    def summary(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        return ops.summary()

    @app.get("/api/v1/topology")
    def topology(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        return ops.topology()

    @app.get("/api/v1/timeline")
    def timeline(
        limit: int = 40, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        return {"events": ops.timeline(limit)}

    @app.get("/api/v1/runs")
    def runs(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        with ops.session() as session:
            rows = session.scalars(
                select(TrainingRun).order_by(TrainingRun.created_at.desc())
            ).all()
            return {"runs": [ops.dump_run(row) for row in rows]}

    @app.get("/api/v1/runs/compare")
    def compare(
        ids: str, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        return ops.compare_runs([item for item in ids.split(",") if item])

    @app.get("/api/v1/runs/{run_id}")
    def run_detail(
        run_id: str, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        with ops.session() as session:
            row = session.get(TrainingRun, run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Training run was not found.")
            return ops.dump_run(row)

    @app.post("/api/v1/training/jobs")
    def start_training(
        body: TrainingBody,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("train")),
    ) -> dict:
        return ops.start_training(user["sub"], body.reason, body.trigger, body.seed, body.runtime)

    @app.get("/api/v1/training/jobs")
    def training_jobs(
        ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        return runs(ops)

    @app.get("/api/v1/training/jobs/{run_id}")
    def training_job(
        run_id: str, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        return run_detail(run_id, ops)

    @app.get("/api/v1/models")
    def models(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        with ops.session() as session:
            rows = session.scalars(select(ModelVersion).order_by(ModelVersion.version.desc())).all()
            return {"versions": [ops.dump_version(row) for row in rows]}

    @app.get("/api/v1/versions/{version_id}")
    def version_detail(
        version_id: str, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        with ops.session() as session:
            row = session.get(ModelVersion, version_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Model version was not found.")
            gates = session.scalars(
                select(GateResult).where(GateResult.model_version_id == version_id)
            ).all()
            return {
                "version": ops.dump_version(row),
                "gates": [
                    {
                        "gate": gate.gate,
                        "passed": gate.passed,
                        "summary": gate.summary,
                        "evidence": gate.evidence,
                        "created_at": gate.created_at.isoformat(),
                    }
                    for gate in gates
                ],
            }

    @app.get("/api/v1/versions/{version_id}/lineage")
    def lineage(
        version_id: str, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        return ops.lineage(version_id)

    @app.get("/api/v1/releases")
    def releases(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        with ops.session() as session:
            rows = session.scalars(select(Release).order_by(Release.created_at.desc())).all()
            return {"releases": [ops.dump_release(session, row) for row in rows]}

    @app.post("/api/v1/releases")
    def create_release(
        body: ReleaseBody,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("release_create")),
    ) -> dict:
        return ops.create_release(user["sub"], body.model_version_id, body.environment)

    @app.get("/api/v1/releases/{release_id}")
    def release_detail(
        release_id: str, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        with ops.session() as session:
            return ops.dump_release(session, ops._release(session, release_id))

    @app.post("/api/v1/releases/{release_id}/approval-request")
    def request_approval(
        release_id: str,
        body: ReasonBody,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("release_operate")),
    ) -> dict:
        return ops.request_approval(user["sub"], release_id, body.reason)

    @app.post("/api/v1/releases/{release_id}/approve")
    def approve(
        release_id: str,
        body: ReasonBody,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("approve")),
    ) -> dict:
        return ops.approve_release(user["sub"], release_id, body.reason)

    @app.post("/api/v1/releases/{release_id}/deploy")
    def deploy(
        release_id: str,
        body: ReasonBody,
        image_digest: str = "",
        ops: Platform = Depends(platform),
        user: dict = Depends(require("release_operate")),
    ) -> dict:
        del body
        return ops.deploy(user["sub"], release_id, image_digest)

    @app.post("/api/v1/releases/{release_id}/canary")
    def canary(
        release_id: str,
        body: CanaryBody,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("release_operate")),
    ) -> dict:
        return ops.set_canary(user["sub"], release_id, body.weight)

    @app.post("/api/v1/releases/{release_id}/promote")
    def promote(
        release_id: str,
        body: ReasonBody,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("approve")),
    ) -> dict:
        return ops.promote(user["sub"], release_id, body.reason)

    @app.post("/api/v1/releases/{release_id}/rollback")
    def rollback(
        release_id: str,
        body: RollbackBody,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("rollback")),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="Idempotency-Key is required for rollback.")
        trigger = "manual" if body.trigger == "manual" else body.trigger
        return ops.rollback(user["sub"], release_id, body.reason, trigger, True, idempotency_key)

    @app.post("/api/v1/releases/{release_id}/cancel")
    def cancel(
        release_id: str,
        body: ReasonBody,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("release_operate")),
    ) -> dict:
        return ops.cancel(user["sub"], release_id, body.reason)

    @app.get("/api/v1/routing")
    def routing(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        return ops.router.snapshot()

    @app.post("/api/v1/traffic/generate")
    def traffic(
        body: TrafficBody, ops: Platform = Depends(platform), _: dict = Depends(require("train"))
    ) -> dict:
        return ops.generate_traffic(body.count, body.scenario, body.label_delay_seconds)

    @app.post("/api/v1/gateway/predict")
    def gateway_predict(
        body: PredictBody, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        return ops.predict_gateway(body.features)

    @app.post("/api/v1/labels/ingest")
    def ingest(
        body: LabelBody, ops: Platform = Depends(platform), _: dict = Depends(require("train"))
    ) -> dict:
        return ops.ingest_labels(body.min_age_seconds)

    @app.get("/api/v1/performance")
    def performance(
        model_version_id: str | None = None,
        ops: Platform = Depends(platform),
        _: dict = Depends(require("read")),
    ) -> dict:
        return ops.performance(model_version_id)

    @app.get("/api/v1/metrics/series")
    def series(
        minutes: int = 30, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        return ops.series(minutes)

    @app.post("/api/v1/reliability/scan")
    def scan(
        ops: Platform = Depends(platform), _: dict = Depends(require("release_operate"))
    ) -> dict:
        return ops.scan()

    @app.get("/api/v1/incidents")
    def incidents(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        with ops.session() as session:
            rows = session.scalars(select(Incident).order_by(Incident.opened_at.desc())).all()
            return {"incidents": [_incident(row) for row in rows]}

    @app.get("/api/v1/incidents/{incident_id}")
    def incident_detail(
        incident_id: str, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        with ops.session() as session:
            row = session.get(Incident, incident_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Incident was not found.")
            return _incident(row)

    @app.post("/api/v1/incidents")
    def open_incident(
        body: IncidentBody,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("release_operate")),
    ) -> dict:
        from kubeml.store import new_id, utcnow

        with ops.session() as session:
            row = Incident(
                id=new_id("inc"),
                trigger=body.trigger,
                severity="medium",
                status="open",
                environment=settings.environment,
                model_version_id=body.model_version_id,
                summary=body.summary,
                evidence={**body.evidence, "opened_by": user["sub"], "causality": "not_inferred"},
                actions=[],
                opened_at=utcnow(),
            )
            session.add(row)
            ops.audit(
                session, user["sub"], "incident.open", row.id, body.summary, {}, {"status": "open"}
            )
            session.commit()
            return _incident(row)

    @app.post("/api/v1/drift/analyze")
    def analyze(
        model_version_id: str | None = None,
        ops: Platform = Depends(platform),
        user: dict = Depends(require("train")),
    ) -> dict:
        return ops.analyse_drift(user["sub"], model_version_id)

    @app.get("/api/v1/drift/runs")
    def drift_runs(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        with ops.session() as session:
            rows = session.scalars(select(DriftRun).order_by(DriftRun.created_at.desc())).all()
            return {"runs": [ops.dump_drift(row) for row in rows]}

    @app.get("/api/v1/drift/runs/{run_id}")
    def drift_detail(
        run_id: str, ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        with ops.session() as session:
            row = session.get(DriftRun, run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Drift run was not found.")
            return ops.dump_drift(row)

    @app.get("/api/v1/policies/current")
    def current_policy(
        ops: Platform = Depends(platform), _: dict = Depends(require("read"))
    ) -> dict:
        with ops.session() as session:
            document = session.scalar(
                select(PolicyDocument)
                .where(PolicyDocument.active.is_(True))
                .order_by(PolicyDocument.version.desc())
            )
            return {
                "version": None if document is None else document.version,
                "body": ops.policy(session),
                "reason": "" if document is None else document.reason,
            }

    @app.get("/api/v1/policies")
    def policies(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        with ops.session() as session:
            rows = session.scalars(
                select(PolicyDocument).order_by(PolicyDocument.version.desc())
            ).all()
            return {
                "policies": [
                    {
                        "id": row.id,
                        "version": row.version,
                        "active": row.active,
                        "actor": row.actor,
                        "reason": row.reason,
                        "created_at": row.created_at.isoformat(),
                        "body": row.body,
                    }
                    for row in rows
                ]
            }

    @app.put("/api/v1/policies")
    def save_policy(
        body: PolicyBody, ops: Platform = Depends(platform), user: dict = Depends(require("policy"))
    ) -> dict:
        return ops.save_policy(user["sub"], body.body, body.reason)

    @app.get("/api/v1/audit")
    def audit(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        with ops.session() as session:
            rows = session.scalars(
                select(AuditEvent).order_by(AuditEvent.at.desc()).limit(200)
            ).all()
            return {
                "events": [
                    {
                        "id": row.id,
                        "at": row.at.isoformat(),
                        "actor": row.actor,
                        "action": row.action,
                        "target": row.target,
                        "reason": row.reason,
                        "previous": row.previous,
                        "new": row.new,
                        "outcome": row.outcome,
                        "release_id": row.release_id,
                        "incident_id": row.incident_id,
                    }
                    for row in rows
                ]
            }

    @app.get("/api/v1/cluster")
    def cluster(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        return ops.cluster_view()

    @app.get("/api/v1/cluster/logs")
    def logs(
        namespace: str,
        pod: str,
        ops: Platform = Depends(platform),
        _: dict = Depends(require("logs")),
    ) -> dict:
        try:
            return ops.logs(namespace, pod)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/faults")
    def get_faults(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        return read_faults(ops.settings.data_dir)

    @app.post("/api/v1/faults")
    def set_faults(
        body: FaultBody, ops: Platform = Depends(platform), user: dict = Depends(require("fault"))
    ) -> dict:
        payload = {key: value for key, value in body.model_dump().items() if value is not None}
        result = ops.set_faults(payload)
        with ops.session() as session:
            ops.audit(session, user["sub"], "fault.set", "serving", "fault injection", {}, payload)
            session.commit()
        return result

    @app.post("/api/v1/images")
    def record_image(
        body: ImageBody, ops: Platform = Depends(platform), user: dict = Depends(require("policy"))
    ) -> dict:
        return ops.record_image(user["sub"], body.digest, body.artifact_sha256, body.repository)

    @app.get("/api/v1/alerts")
    def alerts(ops: Platform = Depends(platform), _: dict = Depends(require("read"))) -> dict:
        with ops.session() as session:
            rows = session.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(50)).all()
            return {
                "alerts": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "status": row.status,
                        "severity": row.severity,
                        "summary": row.summary,
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in rows
                ]
            }

    @app.post("/api/v1/alerts/webhook")
    def alert_webhook(
        payload: dict[str, Any], request: Request, ops: Platform = Depends(platform)
    ) -> dict:
        token = request.headers.get("x-kubeml-alert-token", "")
        bearer = request.headers.get("authorization", "")
        if token != settings.alert_token and bearer != f"Bearer {settings.alert_token}":
            raise HTTPException(status_code=401, detail="Alert token was rejected.")
        return ops.record_alert(payload)

    return app


def _incident(row: Incident) -> dict:
    return {
        "id": row.id,
        "trigger": row.trigger,
        "severity": row.severity,
        "status": row.status,
        "environment": row.environment,
        "model_version_id": row.model_version_id,
        "release_id": row.release_id,
        "summary": row.summary,
        "evidence": row.evidence,
        "actions": row.actions,
        "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


def main() -> None:
    import uvicorn

    uvicorn.run(
        "kubeml.platform.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
