from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from kubeml.config import Settings, get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(200))


class TrainingRun(Base):
    __tablename__ = "training_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    runtime: Mapped[str] = mapped_column(String(32), default="local-thread")
    reason: Mapped[str] = mapped_column(Text, default="")
    trigger: Mapped[str] = mapped_column(String(64), default="manual")
    seed: Mapped[int] = mapped_column(Integer, default=7)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[str] = mapped_column(Text, default="")
    mlflow_run_id: Mapped[str] = mapped_column(String(64), default="")
    mlflow_version: Mapped[str] = mapped_column(String(32), default="")
    tracking_status: Mapped[str] = mapped_column(String(32), default="pending")
    tracking_error: Mapped[str] = mapped_column(Text, default="")
    dataset_fingerprint: Mapped[str] = mapped_column(String(80), default="")
    git_commit: Mapped[str] = mapped_column(String(80), default="")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    artifact_sha256: Mapped[str] = mapped_column(String(80), default="")
    model_version_id: Mapped[str] = mapped_column(String(32), default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    dependencies: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(64), default="")


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    alias: Mapped[str] = mapped_column(String(32), default="candidate")
    approval: Mapped[str] = mapped_column(String(32), default="unreviewed")
    task: Mapped[str] = mapped_column(String(64))
    framework: Mapped[str] = mapped_column(String(64))
    artifact_path: Mapped[str] = mapped_column(Text)
    artifact_sha256: Mapped[str] = mapped_column(String(80))
    artifact_bytes: Mapped[int] = mapped_column(Integer, default=0)
    dataset_fingerprint: Mapped[str] = mapped_column(String(80))
    dataset_rows: Mapped[int] = mapped_column(Integer, default=0)
    schema_version: Mapped[str] = mapped_column(String(80))
    git_commit: Mapped[str] = mapped_column(String(80), default="")
    training_run_id: Mapped[str] = mapped_column(String(32), default="")
    mlflow_run_id: Mapped[str] = mapped_column(String(64), default="")
    mlflow_version: Mapped[str] = mapped_column(String(32), default="")
    image_digest: Mapped[str] = mapped_column(String(120), default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GateResult(Base):
    __tablename__ = "gate_results"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_version_id: Mapped[str] = mapped_column(String(32))
    release_id: Mapped[str] = mapped_column(String(32), default="")
    gate: Mapped[str] = mapped_column(String(32))
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Release(Base):
    __tablename__ = "releases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    environment: Mapped[str] = mapped_column(String(32))
    model_version_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    runtime: Mapped[str] = mapped_column(String(32), default="local-router")
    image_digest: Mapped[str] = mapped_column(String(120), default="")
    artifact_sha256: Mapped[str] = mapped_column(String(80), default="")
    git_commit: Mapped[str] = mapped_column(String(80), default="")
    training_run_id: Mapped[str] = mapped_column(String(32), default="")
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    canary_weight: Mapped[int] = mapped_column(Integer, default=0)
    previous_release_id: Mapped[str] = mapped_column(String(32), default="")
    approval_status: Mapped[str] = mapped_column(String(32), default="pending")
    failure: Mapped[str] = mapped_column(Text, default="")
    failed_stage: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReleaseEvent(Base):
    __tablename__ = "release_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    release_id: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))
    from_state: Mapped[str] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class EnvironmentLock(Base):
    __tablename__ = "environment_locks"
    environment: Mapped[str] = mapped_column(String(32), primary_key=True)
    release_id: Mapped[str] = mapped_column(String(32), default="")
    operation: Mapped[str] = mapped_column(String(64), default="")


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(32), default="medium")
    status: Mapped[str] = mapped_column(String(32), default="open")
    environment: Mapped[str] = mapped_column(String(32), default="local")
    model_version_id: Mapped[str] = mapped_column(String(32), default="")
    release_id: Mapped[str] = mapped_column(String(32), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text, default="")
    previous: Mapped[dict] = mapped_column(JSON, default=dict)
    new: Mapped[dict] = mapped_column(JSON, default=dict)
    outcome: Mapped[str] = mapped_column(String(32), default="succeeded")
    release_id: Mapped[str] = mapped_column(String(32), default="")
    incident_id: Mapped[str] = mapped_column(String(32), default="")


class PolicyDocument(Base):
    __tablename__ = "policies"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Prediction(Base):
    __tablename__ = "predictions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    model_version_id: Mapped[str] = mapped_column(String(32), default="")
    release_id: Mapped[str] = mapped_column(String(32), default="")
    slot: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(32), default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    error_type: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyntheticOutcome(Base):
    __tablename__ = "synthetic_outcomes"
    prediction_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    outcome: Mapped[int] = mapped_column(Integer)
    available_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Label(Base):
    __tablename__ = "labels"
    prediction_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    outcome: Mapped[int] = mapped_column(Integer)
    labeled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(64), default="delayed_ingest")


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_version_id: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(64), default="synthetic_authorised")
    features: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DriftRun(Base):
    __tablename__ = "drift_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_version_id: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(String(32))
    reference_rows: Mapped[int] = mapped_column(Integer, default=0)
    current_rows: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(32), default="warning")
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def build_engine(settings: Settings | None = None):
    settings = settings or get_settings()
    connect_args = {"check_same_thread": False} if settings.sqlite else {}
    engine = create_engine(settings.database_url, future=True, connect_args=connect_args)
    if settings.sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite(connection, _record) -> None:  # noqa: ANN001
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def session_factory(settings: Settings | None = None):
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
