from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from kubeml.adapter import InputValidationError
from kubeml.auth import hash_password
from kubeml.cluster import pod_logs, probe_cluster
from kubeml.config import Settings
from kubeml.dataset import sample_requests
from kubeml.drift import analyse_frame, population_stability_index
from kubeml.engine import TrafficRouter, load_engine, read_faults, write_faults
from kubeml.gates import run_gates
from kubeml.images import read_image_build, write_image_build
from kubeml.metrics import (
    DEPLOYMENTS,
    GATE_FAILURES,
    INFERENCE_DURATION,
    INFERENCE_ERRORS,
    INFERENCE_REQUESTS,
    LAST_SUCCESS,
    MODEL_LOADED,
    ROLLBACKS,
    TRAINING_DURATION,
    TRAINING_JOBS,
)
from kubeml.releases import (
    assert_transition,
    evaluate_canary,
    rollback_authorised,
    stage_status,
    summarise_routes,
)
from kubeml.schema import (
    FEATURE_NAMES,
    NEGATIVE_LABEL,
    POSITIVE_LABEL,
    SCHEMA_VERSION,
    default_policy,
)
from kubeml.store import (
    Alert,
    AuditEvent,
    DriftRun,
    EnvironmentLock,
    GateResult,
    Incident,
    Label,
    ModelVersion,
    Observation,
    Operation,
    PolicyDocument,
    Prediction,
    Release,
    ReleaseEvent,
    SyntheticOutcome,
    TrainingRun,
    User,
    new_id,
    session_factory,
    utcnow,
)
from kubeml.training import log_mlflow, train_credit_risk


class PlatformError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _container_identity(
    recorded: str, artifact_sha256: str, build: dict[str, Any]
) -> tuple[str, str, str]:
    built = str(build.get("digest") or "")
    built_sha = str(build.get("artifact_sha256") or "")
    if recorded:
        return recorded, "healthy", f"Recorded image {recorded}."
    if built and built_sha and built_sha == artifact_sha256:
        return built, "healthy", f"Local image id {built} matches the loaded artifact."
    if built:
        other = built_sha[:12] if built_sha else "unknown"
        return (
            "",
            "idle",
            f"Local image {built} contains artifact {other}, which is not the loaded model.",
        )
    return "", "idle", "No image digest recorded. Local inference is using the JSON artifact."


def _monitoring_summary(has_recent: bool, latest: Any) -> str:
    if has_recent:
        return "operational database"
    if latest is None:
        return "No requests in the last 5 minutes. No earlier measurement is stored."
    return f"No requests in the last 5 minutes. Last measurement {iso(latest.created_at)}."


def _split_line(split: Any) -> str:
    if not isinstance(split, dict):
        return f"Split {split}"
    parts = [f"{name} {count}" for name, count in split.items()]
    return "Split " + ", ".join(parts)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class Platform:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.Session = session_factory(settings)
        self.router = TrafficRouter()
        self._guards = defaultdict(threading.Lock)
        self._threads: list[threading.Thread] = []
        self.bootstrap()

    def session(self) -> Session:
        return self.Session()

    def bootstrap(self) -> None:
        with self.session() as session:
            self._seed_users(session)
            self._seed_policy(session)
            champion = session.scalar(select(ModelVersion).where(ModelVersion.alias == "champion"))
            if champion is None:
                trained = train_credit_risk(self.settings, seed=self.settings.dataset_seed)
                tracking = self._track(trained, "champion")
                version = self._add_version(
                    session,
                    trained,
                    tracking,
                    alias="champion",
                    approval="approved",
                    actor="system",
                )
                gates = self._persist_gates(session, version, trained, champion_metrics=None)
                if not all(item["passed"] for item in gates):
                    version.alias = "rejected"
                    version.approval = "rejected"
                    session.commit()
                    raise PlatformError(
                        "Bootstrap model failed quality gates. Nothing was marked champion."
                    )
                release = Release(
                    id=new_id("rel"),
                    environment=self.settings.environment,
                    model_version_id=version.id,
                    status="promoted",
                    runtime="local-router",
                    artifact_sha256=version.artifact_sha256,
                    git_commit=version.git_commit,
                    training_run_id=version.training_run_id,
                    config_version=1,
                    canary_weight=0,
                    approval_status="approved",
                )
                session.add(release)
                session.add(
                    ReleaseEvent(
                        id=new_id("evt"),
                        release_id=release.id,
                        actor="system",
                        from_state="",
                        to_state="promoted",
                        reason="Initial local-router pin after quality gates. No previous release existed, so no canary ran.",
                        evidence={
                            "bootstrap": True,
                            "canary": "not_applicable",
                            "runtime": "local-router",
                        },
                    )
                )
                run = session.get(TrainingRun, version.training_run_id)
                if run is not None:
                    run.status = "succeeded"
                    run.finished_at = utcnow()
                    run.started_at = run.started_at or utcnow()
                session.commit()
                champion = version
            else:
                session.commit()
            self._load_stable(champion)
            self._restore_canary()

    def _seed_users(self, session: Session) -> None:
        if session.scalar(select(User.id).limit(1)):
            return
        people = [
            ("admin", "Platform administrator", "admin"),
            ("engineer", "ML engineer", "engineer"),
            ("approver", "Release approver", "approver"),
            ("observer", "Read-only observer", "observer"),
        ]
        for username, name, role in people:
            session.add(
                User(
                    id=new_id("usr"),
                    username=username,
                    name=name,
                    role=role,
                    password_hash=hash_password("kubeml-local"),
                )
            )

    def _seed_policy(self, session: Session) -> None:
        if session.scalar(select(PolicyDocument.id).limit(1)):
            return
        session.add(
            PolicyDocument(
                id=new_id("pol"),
                version=1,
                body=default_policy(),
                actor="system",
                reason="Default local policy.",
                active=True,
            )
        )

    def policy(self, session: Session) -> dict[str, Any]:
        document = session.scalar(
            select(PolicyDocument)
            .where(PolicyDocument.active.is_(True))
            .order_by(PolicyDocument.version.desc())
        )
        if document is None:
            return default_policy()
        return document.body

    def policy_version(self, session: Session) -> int:
        document = session.scalar(
            select(PolicyDocument)
            .where(PolicyDocument.active.is_(True))
            .order_by(PolicyDocument.version.desc())
        )
        return 0 if document is None else document.version

    def _track(self, trained: dict[str, Any], alias: str | None) -> dict[str, Any]:
        try:
            logged = log_mlflow(self.settings, trained, alias)
            return {"status": "mlflow", "error": "", **logged}
        except Exception as exc:  # noqa: BLE001 - tracking failure must stay visible
            if self.settings.mlflow_required:
                raise
            return {
                "status": "local",
                "error": str(exc),
                "run_id": "",
                "registry_version": "",
                "alias": alias,
            }

    def _add_version(
        self,
        session: Session,
        trained: dict[str, Any],
        tracking: dict[str, Any],
        alias: str,
        approval: str,
        actor: str,
        trigger: str = "bootstrap",
        reason: str = "bootstrap",
    ) -> ModelVersion:
        current = session.scalar(
            select(ModelVersion.version).order_by(ModelVersion.version.desc()).limit(1)
        )
        number = int(current or 0) + 1
        run_id = new_id("trn")
        version_id = new_id("mdl")
        session.add(
            TrainingRun(
                id=run_id,
                status="succeeded",
                runtime="local-thread",
                reason=reason,
                trigger=trigger,
                seed=trained["seed"],
                started_at=utcnow(),
                finished_at=utcnow(),
                mlflow_run_id=tracking.get("run_id", ""),
                mlflow_version=tracking.get("registry_version", ""),
                tracking_status=tracking.get("status", "local"),
                tracking_error=tracking.get("error", ""),
                dataset_fingerprint=trained["dataset_fingerprint"],
                git_commit=trained["git_commit"],
                artifact_path=trained["artifact_path"],
                artifact_sha256=trained["artifact_sha256"],
                model_version_id=version_id,
                params=trained["params"],
                metrics=trained["metrics"],
                dependencies=trained["dependencies"],
                log=self._training_log(trained, tracking),
                actor=actor,
            )
        )
        version = ModelVersion(
            id=version_id,
            name=self.settings.model_name,
            version=number,
            alias=alias,
            approval=approval,
            task="binary_classification",
            framework="scikit-learn",
            artifact_path=trained["artifact_path"],
            artifact_sha256=trained["artifact_sha256"],
            artifact_bytes=trained["artifact_bytes"],
            dataset_fingerprint=trained["dataset_fingerprint"],
            dataset_rows=trained["dataset_rows"],
            schema_version=trained["schema_version"],
            git_commit=trained["git_commit"],
            training_run_id=run_id,
            mlflow_run_id=tracking.get("run_id", ""),
            mlflow_version=tracking.get("registry_version", ""),
            metrics=trained["metrics"],
            params=trained["params"],
        )
        session.add(version)
        session.flush()
        return version

    def _training_log(self, trained: dict[str, Any], tracking: dict[str, Any]) -> str:
        validation = trained["metrics"]["validation"]
        lines = [
            f"Dataset fingerprint {trained['dataset_fingerprint']}",
            f"Rows {trained['dataset_rows']}",
            _split_line(trained["split"]),
            f"Validation ROC AUC {validation.get('roc_auc')} F1 {validation.get('f1')}",
            f"Artifact sha256 {trained['artifact_sha256']}",
            f"Tracking {tracking.get('status')} {tracking.get('error', '')}",
            f"Git commit {trained['git_commit']}",
        ]
        return "\n".join(lines) + "\n"

    def record_image(
        self, actor: str, digest: str, artifact_sha256: str, repository: str
    ) -> dict[str, Any]:
        if not digest.startswith("sha256:") or len(digest) < 15:
            raise PlatformError("Image digest must be a sha256 identifier from a completed build.")
        payload = write_image_build(self.settings.data_dir, digest, artifact_sha256, repository)
        matched: list[str] = []
        with self.session() as session:
            versions = session.scalars(
                select(ModelVersion).where(ModelVersion.artifact_sha256 == artifact_sha256)
            ).all()
            for version in versions:
                version.image_digest = digest
                matched.append(version.id)
                releases = session.scalars(
                    select(Release).where(Release.model_version_id == version.id)
                ).all()
                for release in releases:
                    if not release.image_digest:
                        release.image_digest = digest
            self.audit(
                session,
                actor,
                "image.record",
                repository or "image",
                "Recorded a local image id against the artifact it contains.",
                {},
                {"digest": digest, "matched_versions": matched},
            )
            session.commit()
        return {"build": payload, "matched_versions": matched}

    def _persist_gates(
        self,
        session: Session,
        version: ModelVersion,
        trained: dict[str, Any],
        champion_metrics: dict[str, Any] | None,
        release_id: str = "",
    ) -> list[dict[str, Any]]:
        model = trained["model"]
        outcomes = run_gates(
            trained["full_frame"],
            trained["metrics"],
            model,
            champion_metrics,
            self.policy(session),
            version.artifact_sha256,
        )
        stored = []
        for outcome in outcomes:
            session.add(
                GateResult(
                    id=new_id("gate"),
                    model_version_id=version.id,
                    release_id=release_id,
                    gate=outcome["gate"],
                    passed=outcome["passed"],
                    summary=outcome["summary"],
                    evidence=outcome["evidence"],
                )
            )
            if not outcome["passed"]:
                GATE_FAILURES.labels(gate=outcome["gate"]).inc()
            stored.append(outcome)
        return stored

    def _load_stable(self, version: ModelVersion) -> None:
        self.router.stable = load_engine(version.artifact_path, version.id, version.version)
        self.router.weight = 0
        MODEL_LOADED.labels(service="local-router", slot="stable").set(1)

    def _restore_canary(self) -> None:
        with self.session() as session:
            release = session.scalar(
                select(Release)
                .where(Release.status.in_(["canary", "healthy", "deploying"]))
                .order_by(Release.updated_at.desc())
            )
            if release is None:
                return
            version = session.get(ModelVersion, release.model_version_id)
            if version is None:
                return
            self.router.candidate = load_engine(version.artifact_path, version.id, version.version)
            self.router.weight = release.canary_weight
            MODEL_LOADED.labels(service="local-router", slot="candidate").set(1)

    def audit(
        self,
        session: Session,
        actor: str,
        action: str,
        target: str,
        reason: str,
        previous: dict,
        new: dict,
        outcome: str = "succeeded",
        release_id: str = "",
        incident_id: str = "",
    ) -> None:
        session.add(
            AuditEvent(
                id=new_id("aud"),
                actor=actor,
                action=action,
                target=target,
                reason=reason,
                previous=previous,
                new=new,
                outcome=outcome,
                release_id=release_id,
                incident_id=incident_id,
            )
        )

    def start_training(
        self, actor: str, reason: str, trigger: str, seed: int | None, runtime: str
    ) -> dict[str, Any]:
        if runtime not in {"local-thread", "kubernetes"}:
            raise PlatformError("Training runtime must be local-thread or kubernetes.")
        with self.session() as session:
            if runtime == "kubernetes":
                cluster = probe_cluster()
                if not cluster["connected"]:
                    run = TrainingRun(
                        id=new_id("trn"),
                        status="failed",
                        runtime="kubernetes",
                        reason=reason,
                        trigger=trigger,
                        seed=seed or self.settings.dataset_seed,
                        finished_at=utcnow(),
                        failure=f"Kubernetes training was not submitted. {cluster['reason']}",
                        log="No Job was created.\n",
                        actor=actor,
                    )
                    session.add(run)
                    self.audit(
                        session,
                        actor,
                        "training.start",
                        run.id,
                        reason,
                        {},
                        {"status": "failed", "runtime": "kubernetes"},
                        "failed",
                    )
                    session.commit()
                    TRAINING_JOBS.labels(status="failed", runtime="kubernetes").inc()
                    return self.dump_run(run)
            run = TrainingRun(
                id=new_id("trn"),
                status="queued",
                runtime="local-thread",
                reason=reason,
                trigger=trigger,
                seed=seed or self.settings.dataset_seed,
                actor=actor,
                log="Queued. The request returned before training finished.\n",
            )
            session.add(run)
            self.audit(
                session,
                actor,
                "training.start",
                run.id,
                reason,
                {},
                {"status": "queued"},
                "accepted",
            )
            session.commit()
            payload = self.dump_run(run)
        thread = threading.Thread(target=self._execute_training, args=(payload["id"],), daemon=True)
        thread.start()
        return payload

    def _execute_training(self, run_id: str) -> None:
        started = time.perf_counter()
        with self.session() as session:
            run = session.get(TrainingRun, run_id)
            if run is None:
                return
            run.status = "running"
            run.started_at = utcnow()
            run.log += "Training thread started.\n"
            seed = run.seed
            actor = run.actor
            reason = run.reason
            trigger = run.trigger
            session.commit()
        try:
            trained = train_credit_risk(self.settings, seed=seed)
            tracking = self._track(trained, "candidate")
            with self.session() as session:
                champion = session.scalar(
                    select(ModelVersion).where(ModelVersion.alias == "champion")
                )
                champion_metrics = None if champion is None else champion.metrics
                version = self._add_version(
                    session,
                    trained,
                    tracking,
                    alias="candidate",
                    approval="unreviewed",
                    actor=actor,
                    trigger=trigger,
                    reason=reason,
                )
                # The queued row stays the public job id. Copy results onto it and drop the duplicate bookkeeping row's identity by updating the queued run in place.
                duplicate = session.get(TrainingRun, version.training_run_id)
                run = session.get(TrainingRun, run_id)
                assert run is not None and duplicate is not None
                run.status = "succeeded"
                run.finished_at = utcnow()
                run.mlflow_run_id = duplicate.mlflow_run_id
                run.mlflow_version = duplicate.mlflow_version
                run.tracking_status = duplicate.tracking_status
                run.tracking_error = duplicate.tracking_error
                run.dataset_fingerprint = duplicate.dataset_fingerprint
                run.git_commit = duplicate.git_commit
                run.artifact_path = duplicate.artifact_path
                run.artifact_sha256 = duplicate.artifact_sha256
                run.model_version_id = version.id
                run.params = duplicate.params
                run.metrics = duplicate.metrics
                run.dependencies = duplicate.dependencies
                run.log += duplicate.log
                version.training_run_id = run.id
                session.delete(duplicate)
                gates = self._persist_gates(session, version, trained, champion_metrics)
                passed = all(item["passed"] for item in gates)
                version.alias = "candidate" if passed else "rejected"
                version.approval = "eligible" if passed else "rejected"
                run.log += (
                    "Quality gates passed.\n"
                    if passed
                    else "Quality gates failed. The model was not marked eligible.\n"
                )
                self.audit(
                    session,
                    actor or "system",
                    "training.finish",
                    run.id,
                    reason,
                    {},
                    {"status": run.status, "model_version_id": version.id, "gates_passed": passed},
                )
                session.commit()
            TRAINING_JOBS.labels(status="succeeded", runtime="local-thread").inc()
            TRAINING_DURATION.labels(status="succeeded").observe(time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001
            with self.session() as session:
                run = session.get(TrainingRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.failure = str(exc)
                    run.finished_at = utcnow()
                    run.log += f"Failed: {exc}\n"
                    session.commit()
            TRAINING_JOBS.labels(status="failed", runtime="local-thread").inc()
            TRAINING_DURATION.labels(status="failed").observe(time.perf_counter() - started)

    def create_release(self, actor: str, model_version_id: str, environment: str) -> dict[str, Any]:
        if environment not in {"local", "staging"}:
            raise PlatformError(
                "Supported environments are local and staging. Production promotion is a separate approved step."
            )
        with self._guards[environment], self.session() as session:
            version = session.get(ModelVersion, model_version_id)
            if version is None:
                raise PlatformError("Model version was not found.", 404)
            if version.approval != "eligible" and version.alias != "champion":
                raise PlatformError("This version has not passed quality gates.")
            active = session.scalar(
                select(Release).where(
                    Release.environment == environment,
                    Release.status.in_(["deploying", "canary", "healthy", "awaiting_approval"]),
                )
            )
            if active is not None:
                raise PlatformError(f"Release {active.id} is already active in {environment}.", 409)
            previous = session.scalar(
                select(Release)
                .where(Release.environment == environment, Release.status == "promoted")
                .order_by(Release.created_at.desc())
            )
            release = Release(
                id=new_id("rel"),
                environment=environment,
                model_version_id=version.id,
                status="validated",
                runtime="kubernetes" if environment == "staging" else "local-router",
                artifact_sha256=version.artifact_sha256,
                image_digest=version.image_digest or "",
                git_commit=version.git_commit,
                training_run_id=version.training_run_id,
                config_version=self.policy_version(session),
                previous_release_id="" if previous is None else previous.id,
                approval_status="pending",
            )
            session.add(release)
            session.add(
                ReleaseEvent(
                    id=new_id("evt"),
                    release_id=release.id,
                    actor=actor,
                    from_state="candidate",
                    to_state="validated",
                    reason="Stored quality gates passed. Release is eligible to deploy.",
                    evidence={"gates": "stored"},
                )
            )
            self.audit(
                session,
                actor,
                "release.create",
                release.id,
                "create",
                {},
                {"status": "validated", "model_version_id": version.id},
            )
            session.commit()
            return self.dump_release(session, release)

    def _transition(
        self,
        session: Session,
        release: Release,
        target: str,
        actor: str,
        reason: str,
        evidence: dict | None = None,
    ) -> None:
        assert_transition(release.status, target)
        previous = release.status
        release.status = target
        release.updated_at = utcnow()
        session.add(
            ReleaseEvent(
                id=new_id("evt"),
                release_id=release.id,
                actor=actor,
                from_state=previous,
                to_state=target,
                reason=reason,
                evidence=evidence or {},
            )
        )

    def request_approval(self, actor: str, release_id: str, reason: str) -> dict[str, Any]:
        with self.session() as session:
            release = self._release(session, release_id)
            self._transition(
                session,
                release,
                "awaiting_approval",
                actor,
                reason or "Requested promotion approval.",
            )
            release.approval_status = "awaiting_approval"
            self.audit(
                session,
                actor,
                "release.request_approval",
                release.id,
                reason,
                {},
                {"status": release.status},
            )
            session.commit()
            return self.dump_release(session, release)

    def approve_release(self, actor: str, release_id: str, reason: str) -> dict[str, Any]:
        with self.session() as session:
            release = self._release(session, release_id)
            previous = release.approval_status
            release.approval_status = "approved"
            if release.status == "validated":
                self._transition(
                    session,
                    release,
                    "awaiting_approval",
                    actor,
                    reason or "Approved for deployment.",
                )
            elif release.status != "awaiting_approval" and release.status != "healthy":
                raise PlatformError(
                    "Approval is available for a validated, awaiting, or healthy release."
                )
            self.audit(
                session,
                actor,
                "release.approve",
                release.id,
                reason,
                {"approval_status": previous},
                {"approval_status": "approved"},
            )
            session.commit()
            return self.dump_release(session, release)

    def deploy(self, actor: str, release_id: str, image_digest: str = "") -> dict[str, Any]:
        with self.session() as session:
            release = self._release(session, release_id)
            environment = release.environment
        with self._guards[environment]:
            with self.session() as session:
                release = self._release(session, release_id)
                self._acquire(session, release.environment, release.id, "deploy")
                try:
                    if release.environment == "staging":
                        cluster = probe_cluster()
                        if not cluster["connected"]:
                            raise PlatformError(
                                f"Staging deployment was not started. {cluster['reason']}"
                            )
                        if not image_digest:
                            raise PlatformError(
                                "Staging deployment requires an immutable image digest. No deployment was marked successful."
                            )
                        if release.approval_status != "approved":
                            raise PlatformError("Staging deployment requires recorded approval.")
                    if release.status == "validated" and release.environment == "local":
                        self._transition(
                            session,
                            release,
                            "deploying",
                            actor,
                            "Pinning the candidate artifact into the local router.",
                        )
                    elif (
                        release.status == "awaiting_approval"
                        and release.approval_status == "approved"
                    ):
                        self._transition(
                            session, release, "deploying", actor, "Approved deployment started."
                        )
                    elif release.status != "deploying":
                        raise PlatformError(f"Release is {release.status} and cannot deploy.")
                    version = session.get(ModelVersion, release.model_version_id)
                    assert version is not None
                    chosen_digest = image_digest or version.image_digest
                    if chosen_digest:
                        release.image_digest = chosen_digest
                        version.image_digest = chosen_digest
                    engine = load_engine(version.artifact_path, version.id, version.version)
                    smoke = self._smoke_engine(engine)
                    if not smoke["passed"]:
                        release.failure = "Candidate smoke test failed."
                        release.failed_stage = "stage"
                        self._transition(
                            session,
                            release,
                            "failed",
                            actor,
                            "Smoke test failed. Candidate was not given traffic.",
                            smoke,
                        )
                        DEPLOYMENTS.labels(environment=release.environment, result="failed").inc()
                        self._release_lock(session, release.environment, release.id)
                        session.commit()
                        return self.dump_release(session, release)
                    self.router.candidate = engine
                    self.router.weight = 0
                    MODEL_LOADED.labels(service="local-router", slot="candidate").set(1)
                    release.canary_weight = 0
                    self._transition(
                        session,
                        release,
                        "canary",
                        actor,
                        "Candidate loaded and verified with smoke inputs. Traffic weight is 0.",
                        smoke,
                    )
                    DEPLOYMENTS.labels(environment=release.environment, result="canary").inc()
                    self.audit(
                        session,
                        actor,
                        "release.deploy",
                        release.id,
                        "deploy",
                        {},
                        {
                            "status": "canary",
                            "image_digest": release.image_digest,
                            "sha256": release.artifact_sha256,
                        },
                    )
                    self._release_lock(session, release.environment, release.id)
                    session.commit()
                    return self.dump_release(session, release)
                except Exception:
                    self._release_lock(session, release.environment, release.id)
                    session.commit()
                    raise

    def set_canary(self, actor: str, release_id: str, weight: int) -> dict[str, Any]:
        if weight not in {0, 5, 20, 50, 100}:
            raise PlatformError("Canary weight must be one of 0, 5, 20, 50, 100.")
        with self.session() as session:
            release = self._release(session, release_id)
            if release.status not in {"canary", "healthy"}:
                raise PlatformError("Canary weight changes require a loaded candidate.")
            policy = self.policy(session)
            previous_weight = release.canary_weight
            if weight > previous_weight and previous_weight > 0:
                stats = self._stats_for_release(session, release)
                decision = evaluate_canary(
                    stats, policy, self.router.stable is not None, self.router.candidate is not None
                )
                if decision["decision"] != "passing":
                    raise PlatformError(
                        f"Canary was not advanced. Decision: {decision['decision']}."
                    )
            if weight == 100 and release.status == "canary":
                stats = self._stats_for_release(session, release)
                decision = evaluate_canary(stats, policy, True, self.router.candidate is not None)
                if decision["decision"] != "passing":
                    raise PlatformError(
                        "Full candidate traffic was refused because verification did not pass."
                    )
                self._transition(
                    session,
                    release,
                    "healthy",
                    actor,
                    "Candidate took the full measured window and passed the canary policy.",
                    decision,
                )
            release.canary_weight = weight
            self.router.weight = weight
            self.audit(
                session,
                actor,
                "release.canary",
                release.id,
                f"weight {weight}",
                {},
                {"canary_weight": self.router.weight},
            )
            session.commit()
            return self.dump_release(session, release)

    def promote(self, actor: str, release_id: str, reason: str) -> dict[str, Any]:
        with self.session() as session:
            release = self._release(session, release_id)
            environment = release.environment
        with self._guards[environment]:
            with self.session() as session:
                release = self._release(session, release_id)
                if release.approval_status != "approved":
                    raise PlatformError("Promotion requires an approver to record approval.")
                if release.status != "healthy":
                    raise PlatformError("Promotion requires a healthy canary verification.")
                policy = self.policy(session)
                stats = self._stats_for_release(session, release)
                decision = evaluate_canary(
                    stats, policy, self.router.stable is not None, self.router.candidate is not None
                )
                if decision["decision"] != "passing":
                    raise PlatformError(f"Promotion refused. Decision: {decision['decision']}.")
                self._acquire(session, environment, release.id, "promote")
                version = session.get(ModelVersion, release.model_version_id)
                assert version is not None
                smoke = self._smoke_engine(
                    self.router.candidate
                    or load_engine(version.artifact_path, version.id, version.version)
                )
                if not smoke["passed"]:
                    self._release_lock(session, environment, release.id)
                    session.commit()
                    raise PlatformError("Promotion smoke test failed. Champion was not changed.")
                previous = session.scalar(
                    select(ModelVersion).where(ModelVersion.alias == "champion")
                )
                if previous is not None and previous.id != version.id:
                    previous.alias = "previous"
                version.alias = "champion"
                version.approval = "approved"
                self.router.stable = self.router.candidate or load_engine(
                    version.artifact_path, version.id, version.version
                )
                self.router.candidate = None
                self.router.weight = 0
                release.canary_weight = 0
                MODEL_LOADED.labels(service="local-router", slot="candidate").set(0)
                self._transition(
                    session,
                    release,
                    "promoted",
                    actor,
                    reason or "Promoted after canary verification.",
                    {
                        "decision": decision,
                        "sha256": version.artifact_sha256,
                        "image_digest": release.image_digest,
                    },
                )
                self.audit(
                    session,
                    actor,
                    "release.promote",
                    release.id,
                    reason,
                    {},
                    {"status": "promoted", "model_version_id": version.id},
                )
                self._release_lock(session, environment, release.id)
                session.commit()
                self._sync_alias(version, "champion")
                return self.dump_release(session, release)

    def rollback(
        self,
        actor: str,
        release_id: str,
        reason: str,
        trigger: str,
        manual: bool,
        operation_id: str,
    ) -> dict[str, Any]:
        with self.session() as session:
            existing = session.get(Operation, operation_id)
            if existing is not None:
                return existing.result
            release = self._release(session, release_id)
            environment = release.environment
        with self._guards[environment]:
            with self.session() as session:
                existing = session.get(Operation, operation_id)
                if existing is not None:
                    return existing.result
                release = self._release(session, release_id)
                policy = self.policy(session)
                stats = self._stats_for_release(session, release)
                evidence = {
                    "stats": stats,
                    "trigger": trigger,
                    "requests": stats["candidate"]["requests"],
                }
                decision = rollback_authorised(trigger, evidence, policy, manual)
                if not decision["authorised"]:
                    raise PlatformError(decision["reason"])
                incident = Incident(
                    id=new_id("inc"),
                    trigger=trigger,
                    severity="high" if trigger != "feature_drift" else "medium",
                    status="open",
                    environment=release.environment,
                    model_version_id=release.model_version_id,
                    release_id=release.id,
                    summary=reason or decision["reason"],
                    evidence={"decision": decision, "stats": stats},
                    actions=[],
                )
                session.add(incident)
                self._acquire(session, environment, release.id, "rollback")
                self.router.weight = 0
                self.router.candidate = None
                release.canary_weight = 0
                MODEL_LOADED.labels(service="local-router", slot="candidate").set(0)
                stable_ok = False
                smoke: dict[str, Any] = {"passed": False, "summary": "Stable model is not loaded."}
                if self.router.stable is not None:
                    smoke = self._smoke_engine(self.router.stable)
                    stable_ok = smoke["passed"]
                incident.actions = [
                    {"action": "shift_weight", "weight": 0},
                    {"action": "unload_candidate"},
                    {"action": "smoke_stable", "passed": stable_ok},
                ]
                if not stable_ok:
                    incident.status = "unresolved"
                    release.failure = "Rollback smoke test did not verify the stable model."
                    if release.status != "failed":
                        try:
                            self._transition(
                                session,
                                release,
                                "failed",
                                actor,
                                "Rollback could not verify the stable service.",
                                smoke,
                            )
                        except Exception:
                            release.status = "failed"
                    result = {
                        "release_id": release.id,
                        "status": release.status,
                        "incident_id": incident.id,
                        "verified": False,
                    }
                    ROLLBACKS.labels(environment=environment, result="unverified").inc()
                else:
                    if release.status != "rolled_back":
                        self._transition(
                            session,
                            release,
                            "rolled_back",
                            actor,
                            reason or decision["reason"],
                            {"smoke": smoke, "incident_id": incident.id},
                        )
                    version = session.get(ModelVersion, release.model_version_id)
                    if version is not None and version.alias == "champion":
                        version.alias = "previous"
                        previous_release = (
                            session.get(Release, release.previous_release_id)
                            if release.previous_release_id
                            else None
                        )
                        if previous_release is not None:
                            restored = session.get(ModelVersion, previous_release.model_version_id)
                            if restored is not None:
                                restored.alias = "champion"
                                self.router.stable = load_engine(
                                    restored.artifact_path, restored.id, restored.version
                                )
                    incident.status = "resolved"
                    incident.resolved_at = utcnow()
                    result = {
                        "release_id": release.id,
                        "status": release.status,
                        "incident_id": incident.id,
                        "verified": True,
                    }
                    ROLLBACKS.labels(environment=environment, result="verified").inc()
                self.audit(
                    session,
                    actor,
                    "release.rollback",
                    release.id,
                    reason,
                    {},
                    result,
                    "succeeded" if stable_ok else "unverified",
                    release.id,
                    incident.id,
                )
                self._release_lock(session, environment, release.id)
                session.add(
                    Operation(
                        id=operation_id,
                        kind="rollback",
                        target=release.id,
                        status="recorded",
                        result=result,
                    )
                )
                session.commit()
                return result

    def cancel(self, actor: str, release_id: str, reason: str) -> dict[str, Any]:
        with self.session() as session:
            release = self._release(session, release_id)
            self._transition(session, release, "cancelled", actor, reason or "Cancelled.")
            if (
                self.router.candidate
                and self.router.candidate.model_version_id == release.model_version_id
            ):
                self.router.candidate = None
                self.router.weight = 0
            self.audit(
                session, actor, "release.cancel", release.id, reason, {}, {"status": "cancelled"}
            )
            session.commit()
            return self.dump_release(session, release)

    def _sync_alias(self, version: ModelVersion, alias: str) -> None:
        if not version.mlflow_version:
            return
        try:
            import mlflow
            from mlflow.tracking import MlflowClient

            mlflow.set_tracking_uri(self.settings.mlflow_tracking_uri)
            MlflowClient(tracking_uri=self.settings.mlflow_tracking_uri).set_registered_model_alias(
                self.settings.model_name, alias, version.mlflow_version
            )
        except Exception:
            return

    def _smoke_engine(self, engine) -> dict[str, Any]:
        from kubeml.gates import gate_smoke

        return gate_smoke(engine.model)

    def _acquire(self, session: Session, environment: str, release_id: str, operation: str) -> None:
        row = session.get(EnvironmentLock, environment)
        if row and row.release_id and row.release_id != release_id:
            raise PlatformError(
                f"{environment} is locked by {row.release_id} for {row.operation}.", 409
            )
        if row is None:
            session.add(
                EnvironmentLock(environment=environment, release_id=release_id, operation=operation)
            )
        else:
            row.release_id = release_id
            row.operation = operation
        session.flush()

    def _release_lock(self, session: Session, environment: str, release_id: str) -> None:
        row = session.get(EnvironmentLock, environment)
        if row and row.release_id == release_id:
            row.release_id = ""
            row.operation = ""

    def _release(self, session: Session, release_id: str) -> Release:
        release = session.get(Release, release_id)
        if release is None:
            raise PlatformError("Release was not found.", 404)
        return release

    def generate_traffic(
        self, count: int, scenario: str, label_delay_seconds: int
    ) -> dict[str, Any]:
        if count < 1 or count > 200:
            raise PlatformError("Traffic count must be between 1 and 200.")
        if scenario not in {"reference", "drifted", "missing", "shift_purpose", "invalid"}:
            raise PlatformError("Unknown traffic scenario.")
        records = sample_requests(count, seed=int(time.time()) % 100000 + count, scenario=scenario)
        ok = 0
        errors = 0
        for record in records:
            slot, status = self._gateway_predict(
                record["features"], record["outcome"], scenario, label_delay_seconds
            )
            if status == "ok":
                ok += 1
            else:
                errors += 1
            del slot
        self.scan()
        return {
            "requested": count,
            "ok": ok,
            "errors": errors,
            "scenario": scenario,
            "source": self.router.runtime,
        }

    def _gateway_predict(
        self, features: dict[str, Any], outcome: int | None, scenario: str, label_delay_seconds: int
    ) -> tuple[str, str]:
        slot = self.router.choose()
        started = time.perf_counter()
        status = "ok"
        error_type = ""
        prediction: dict[str, Any] | None = None
        try:
            if self.settings.serving_stable_url:
                import httpx

                base = (
                    self.settings.serving_candidate_url
                    if slot == "candidate"
                    else self.settings.serving_stable_url
                )
                if not base:
                    raise RuntimeError("candidate serving URL is not configured")
                response = httpx.post(f"{base}/predict", json={"features": features}, timeout=10)
                latency_ms = (time.perf_counter() - started) * 1000
                if response.status_code >= 400:
                    status = "error"
                    error_type = "http_status"
                else:
                    prediction = response.json()
                    prediction["latency_ms"] = latency_ms
            else:
                engine = self.router.engine(slot)
                prediction = engine.predict_one(features, self.settings.data_dir)
        except InputValidationError:
            status = "error"
            error_type = "validation"
        except RuntimeError as exc:
            status = "error"
            error_type = str(exc)
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error_type = exc.__class__.__name__
        latency_ms = (
            0
            if prediction is None
            else float(prediction.get("latency_ms") or ((time.perf_counter() - started) * 1000))
        )
        if prediction and prediction.get("label") not in {POSITIVE_LABEL, NEGATIVE_LABEL}:
            status = "error"
            error_type = "invalid_output"
        INFERENCE_REQUESTS.labels(
            service="local-router",
            environment=self.settings.environment,
            endpoint="predict",
            status=status,
            slot=slot,
        ).inc()
        INFERENCE_DURATION.labels(
            service="local-router",
            environment=self.settings.environment,
            endpoint="predict",
            slot=slot,
        ).observe(max(latency_ms, 0) / 1000)
        if status != "ok":
            INFERENCE_ERRORS.labels(
                service="local-router",
                environment=self.settings.environment,
                endpoint="predict",
                error_type=error_type or "error",
                slot=slot,
            ).inc()
        else:
            LAST_SUCCESS.labels(service="local-router", slot=slot).set(time.time())
        with self.session() as session:
            release = session.scalar(
                select(Release)
                .where(Release.status.in_(["canary", "healthy"]))
                .order_by(Release.updated_at.desc())
            )
            promoted = session.scalar(
                select(Release)
                .where(Release.status == "promoted")
                .order_by(Release.created_at.desc())
            )
            model_version_id = ""
            release_id = ""
            if slot == "candidate" and release is not None:
                model_version_id = release.model_version_id
                release_id = release.id
            elif self.router.stable is not None:
                model_version_id = self.router.stable.model_version_id
                release_id = "" if promoted is None else promoted.id
            prediction_id = (
                prediction["prediction_id"]
                if prediction and prediction.get("prediction_id")
                else new_id("prd")
            )
            session.add(
                Prediction(
                    id=prediction_id,
                    model_version_id=model_version_id,
                    release_id=release_id,
                    slot=slot,
                    label="" if not prediction else str(prediction.get("label") or ""),
                    score=None
                    if not prediction or prediction.get("score") is None
                    else float(prediction["score"])
                    if _finite(prediction.get("score"))
                    else None,
                    latency_ms=latency_ms,
                    status=status,
                    error_type=error_type,
                )
            )
            if scenario != "invalid":
                session.add(
                    Observation(
                        id=new_id("obs"),
                        model_version_id=model_version_id
                        or (self.router.stable.model_version_id if self.router.stable else ""),
                        source="synthetic_authorised",
                        features=_plain_features(features),
                    )
                )
                if outcome is not None:
                    session.add(
                        SyntheticOutcome(
                            prediction_id=prediction_id,
                            outcome=int(outcome),
                            available_after=utcnow() + timedelta(seconds=label_delay_seconds),
                        )
                    )
            session.commit()
        return slot, status

    def predict_gateway(self, features: dict[str, Any]) -> dict[str, Any]:
        slot, status = self._gateway_predict(features, None, "reference", 3600)
        if status != "ok":
            raise PlatformError(f"Inference failed: {status}.", 422)
        with self.session() as session:
            row = session.scalar(select(Prediction).order_by(Prediction.created_at.desc()).limit(1))
            return {
                "slot": slot,
                "prediction_id": None if row is None else row.id,
                "label": None if row is None else row.label,
                "score": None if row is None else row.score,
                "model_version_id": None if row is None else row.model_version_id,
            }

    def ingest_labels(self, min_age_seconds: int) -> dict[str, Any]:
        now = utcnow()
        ingested = 0
        with self.session() as session:
            rows = session.scalars(
                select(SyntheticOutcome).where(SyntheticOutcome.ingested_at.is_(None))
            ).all()
            for row in rows:
                if aware(row.available_after) + timedelta(seconds=0) > now and min_age_seconds > 0:
                    continue
                if aware(row.available_after) > now:
                    continue
                session.add(
                    Label(
                        prediction_id=row.prediction_id,
                        outcome=row.outcome,
                        labeled_at=now,
                        source="synthetic_delay",
                    )
                )
                row.ingested_at = now
                ingested += 1
            session.commit()
        return {"ingested": ingested}

    def performance(self, model_version_id: str | None) -> dict[str, Any]:
        with self.session() as session:
            predictions = session.scalars(select(Prediction).where(Prediction.status == "ok")).all()
            if model_version_id:
                predictions = [
                    row for row in predictions if row.model_version_id == model_version_id
                ]
            labels = {row.prediction_id: row for row in session.scalars(select(Label)).all()}
            matched = [(row, labels[row.id]) for row in predictions if row.id in labels]
            coverage = (len(matched) / len(predictions)) if predictions else 0
            if not matched:
                return {
                    "available": False,
                    "reason": "No delayed labels have been ingested for these predictions.",
                    "predictions": len(predictions),
                    "labelled": 0,
                    "coverage": coverage,
                }
            import numpy as np
            from sklearn.metrics import (
                accuracy_score,
                f1_score,
                precision_score,
                recall_score,
                roc_auc_score,
            )

            actual = np.array([item[1].outcome for item in matched])
            scores = np.array(
                [item[0].score if item[0].score is not None else 0.5 for item in matched]
            )
            predicted = (scores >= 0.5).astype(int)
            ages = [
                max(0, (aware(item[1].labeled_at) - aware(item[0].created_at)).total_seconds())
                for item in matched
            ]
            metrics = {
                "available": True,
                "predictions": len(predictions),
                "labelled": len(matched),
                "coverage": coverage,
                "label_age_seconds_mean": float(sum(ages) / len(ages)),
                "accuracy": float(accuracy_score(actual, predicted)),
                "precision": float(precision_score(actual, predicted, zero_division=0)),
                "recall": float(recall_score(actual, predicted, zero_division=0)),
                "f1": float(f1_score(actual, predicted, zero_division=0)),
                "roc_auc": float(roc_auc_score(actual, scores)) if len(set(actual)) > 1 else None,
                "source": "ingested_labels",
            }
            return metrics

    def analyse_drift(self, actor: str, model_version_id: str | None) -> dict[str, Any]:
        with self.session() as session:
            version = (
                session.get(ModelVersion, model_version_id)
                if model_version_id
                else session.scalar(select(ModelVersion).where(ModelVersion.alias == "champion"))
            )
            if version is None:
                raise PlatformError("No model version is available for drift analysis.", 404)
            reference = pd.read_csv(self.settings.data_dir / "reference" / "credit_risk_train.csv")
            observations = session.scalars(
                select(Observation).where(Observation.model_version_id == version.id)
            ).all()
            if len(observations) < 1:
                observations = session.scalars(select(Observation)).all()
            current = (
                pd.DataFrame([row.features for row in observations])
                if observations
                else pd.DataFrame(columns=FEATURE_NAMES)
            )
            report = analyse_frame(
                reference[FEATURE_NAMES],
                current if len(current) else pd.DataFrame(columns=FEATURE_NAMES),
                self.policy(session),
            )
            report["prediction_drift"] = self._prediction_drift(session, version.id)
            run = DriftRun(
                id=new_id("dft"),
                model_version_id=version.id,
                result=report["result"],
                reference_rows=report["reference_rows"],
                current_rows=report["current_rows"],
                report=report,
            )
            session.add(run)
            incident_id = ""
            if report["result"] == "drift":
                incident = Incident(
                    id=new_id("inc"),
                    trigger="feature_drift",
                    severity="medium",
                    status="open",
                    environment=self.settings.environment,
                    model_version_id=version.id,
                    summary="Feature drift exceeded the configured thresholds. Automatic rollback was not authorised.",
                    evidence={
                        "drift_run_id": run.id,
                        "features": report["drifted_features"],
                        "rollback_authorised": False,
                    },
                    actions=[{"action": "recommend", "value": "investigate_or_retrain"}],
                )
                session.add(incident)
                incident_id = incident.id
            self.audit(
                session,
                actor,
                "drift.analyse",
                run.id,
                report["result"],
                {},
                {"result": report["result"]},
                release_id="",
                incident_id=incident_id,
            )
            session.commit()
            return self.dump_drift(run)

    def _prediction_drift(self, session: Session, model_version_id: str) -> dict[str, Any]:
        rows = [
            row
            for row in session.scalars(
                select(Prediction).where(
                    Prediction.model_version_id == model_version_id, Prediction.status == "ok"
                )
            ).all()
            if row.score is not None
        ]
        minimum = int(self.policy(session)["drift"]["min_samples"])
        if len(rows) < minimum * 2:
            return {
                "result": "insufficient_evidence",
                "samples": len(rows),
                "required": minimum * 2,
            }
        rows = sorted(rows, key=lambda row: aware(row.created_at))
        midpoint = len(rows) // 2
        earlier = [row.score for row in rows[:midpoint]]
        later = [row.score for row in rows[midpoint:]]
        psi = population_stability_index(pd.Series(earlier).to_numpy(), pd.Series(later).to_numpy())
        threshold = float(self.policy(session)["drift"]["psi_threshold"])
        return {
            "result": "drift" if psi >= threshold else "stable",
            "psi": psi,
            "threshold": threshold,
            "samples": len(rows),
            "note": "Compares earlier and later score windows. It is not a measure of accuracy.",
        }

    def save_policy(self, actor: str, body: dict[str, Any], reason: str) -> dict[str, Any]:
        required = {"evaluation", "canary", "rollback", "drift", "labels", "serving"}
        if set(body) != required:
            raise PlatformError(
                "Policy must contain evaluation, canary, rollback, drift, labels, and serving."
            )
        if body["rollback"].get("feature_drift_auto_rollback"):
            raise PlatformError("Feature drift cannot be enabled as an automatic rollback trigger.")
        with self.session() as session:
            for document in session.scalars(
                select(PolicyDocument).where(PolicyDocument.active.is_(True))
            ).all():
                document.active = False
            version = self.policy_version(session) + 1
            document = PolicyDocument(
                id=new_id("pol"),
                version=version,
                body=body,
                actor=actor,
                reason=reason,
                active=True,
            )
            session.add(document)
            self.audit(
                session, actor, "policy.update", document.id, reason, {}, {"version": version}
            )
            session.commit()
            return {
                "id": document.id,
                "version": document.version,
                "body": document.body,
                "reason": document.reason,
            }

    def scan(self) -> dict[str, Any]:
        actions = []
        with self.session() as session:
            releases = session.scalars(
                select(Release).where(Release.status.in_(["deploying", "canary"]))
            ).all()
            identifiers = [
                (release.id, release.status, release.updated_at, release.environment)
                for release in releases
            ]
            policy = self.policy(session)
        for release_id, status, updated_at, _environment in identifiers:
            if (
                status == "deploying"
                and (utcnow() - aware(updated_at)).total_seconds()
                > policy["rollback"]["deployment_timeout_seconds"]
            ):
                result = self.rollback(
                    "system",
                    release_id,
                    "Deployment exceeded the configured timeout.",
                    "rollout_timeout",
                    False,
                    f"auto:{release_id}:rollout_timeout",
                )
                actions.append(result)
                continue
            if status == "canary":
                faults = read_faults(self.settings.data_dir)
                if faults.get("not_ready"):
                    result = self.rollback(
                        "system",
                        release_id,
                        "Readiness fault is active.",
                        "readiness_failure",
                        False,
                        f"auto:{release_id}:readiness",
                    )
                    actions.append(result)
                    continue
                with self.session() as session:
                    release = session.get(Release, release_id)
                    if release is None or release.status != "canary":
                        continue
                    stats = self._stats_for_release(session, release)
                    decision = evaluate_canary(
                        stats,
                        policy,
                        self.router.stable is not None,
                        self.router.candidate is not None and not faults.get("not_ready"),
                    )
                if decision["decision"] == "regressing":
                    trigger = _trigger_from_reasons(decision["reasons"])
                    result = self.rollback(
                        "system",
                        release_id,
                        f"Canary regression: {', '.join(decision['reasons'])}.",
                        trigger,
                        False,
                        f"auto:{release_id}:{trigger}",
                    )
                    actions.append(result)
        return {"actions": actions}

    def _stats_for_release(self, session: Session, release: Release) -> dict[str, Any]:
        started = self._canary_started(session, release.id)
        rows = session.scalars(select(Prediction)).all()
        events = []
        for row in rows:
            if started is not None and aware(row.created_at) < started:
                continue
            events.append(
                {
                    "slot": row.slot,
                    "latency_ms": row.latency_ms,
                    "status": row.status,
                    "error_type": row.error_type,
                }
            )
        return summarise_routes(events)

    def _canary_started(self, session: Session, release_id: str) -> datetime | None:
        event = session.scalar(
            select(ReleaseEvent)
            .where(ReleaseEvent.release_id == release_id, ReleaseEvent.to_state == "canary")
            .order_by(ReleaseEvent.at.asc())
        )
        return None if event is None else aware(event.at)

    def series(self, minutes: int = 30) -> dict[str, Any]:
        with self.session() as session:
            rows = session.scalars(select(Prediction)).all()
        cutoff = utcnow() - timedelta(minutes=minutes)
        buckets: dict[str, dict[str, Any]] = {}
        last_at: datetime | None = None
        for row in rows:
            stamp = aware(row.created_at)
            if last_at is None or stamp > last_at:
                last_at = stamp
            if stamp < cutoff:
                continue
            key = stamp.strftime("%H:%M")
            bucket = buckets.setdefault(
                key,
                {"time": key, "at": stamp.isoformat(), "requests": 0, "errors": 0, "latency": []},
            )
            bucket["requests"] += 1
            if row.status != "ok":
                bucket["errors"] += 1
            bucket["latency"].append(row.latency_ms)
        points = []
        for key in sorted(buckets):
            bucket = buckets[key]
            latency = sorted(bucket["latency"])
            p95 = (
                latency[min(len(latency) - 1, int(round((len(latency) - 1) * 0.95)))]
                if latency
                else None
            )
            points.append(
                {
                    "time": key,
                    "at": bucket["at"],
                    "requests": bucket["requests"],
                    "errors": bucket["errors"],
                    "error_rate": bucket["errors"] / bucket["requests"],
                    "p95_ms": p95,
                }
            )
        return {
            "source": "operational_db",
            "window_minutes": minutes,
            "last_measurement_at": None if last_at is None else last_at.isoformat(),
            "points": points,
        }

    def topology(self) -> dict[str, Any]:
        cluster = probe_cluster()
        with self.session() as session:
            versions = session.scalars(
                select(ModelVersion).order_by(ModelVersion.version.desc())
            ).all()
            champion = next((item for item in versions if item.alias == "champion"), None)
            runs = session.scalars(
                select(TrainingRun).order_by(TrainingRun.created_at.desc())
            ).all()
            latest_run = runs[0] if runs else None
            gates = []
            if champion is not None:
                gates = session.scalars(
                    select(GateResult).where(GateResult.model_version_id == champion.id)
                ).all()
            open_incidents = session.scalars(
                select(Incident).where(Incident.status.in_(["open", "unresolved"]))
            ).all()
            predictions = session.scalars(select(Prediction)).all()
            release = session.scalar(
                select(Release)
                .where(Release.status == "promoted")
                .order_by(Release.created_at.desc())
            )
            active = session.scalar(
                select(Release)
                .where(Release.status.in_(["canary", "healthy", "deploying"]))
                .order_by(Release.updated_at.desc())
            )
        recent = [
            row for row in predictions if aware(row.created_at) >= utcnow() - timedelta(minutes=5)
        ]
        errors = [row for row in recent if row.status != "ok"]
        latest_prediction = (
            max(predictions, key=lambda row: aware(row.created_at)) if predictions else None
        )
        gate_ok = bool(gates) and all(item.passed for item in gates)
        image, container_status, container_summary = _container_identity(
            "" if champion is None else champion.image_digest,
            "" if champion is None else champion.artifact_sha256,
            read_image_build(self.settings.data_dir),
        )
        nodes = [
            self._node(
                "dataset",
                "Dataset",
                "Source",
                "healthy" if champion else "unavailable",
                None if champion is None else champion.created_at,
                "credit-risk reference split",
                [
                    ("Rows", str(champion.dataset_rows) if champion else "0"),
                    ("Fingerprint", "" if champion is None else champion.dataset_fingerprint[:12]),
                ],
            ),
            self._node(
                "training",
                "Training",
                "Job",
                _run_status(latest_run),
                None if latest_run is None else latest_run.finished_at or latest_run.created_at,
                "" if latest_run is None else latest_run.status,
                [
                    ("Runtime", "" if latest_run is None else latest_run.runtime),
                    ("Tracking", "" if latest_run is None else latest_run.tracking_status),
                ],
            ),
            self._node(
                "registry",
                "Registry",
                "MLflow",
                "healthy" if champion else "unavailable",
                None if champion is None else champion.created_at,
                "" if champion is None else f"v{champion.version} {champion.alias}",
                [("Versions", str(len(versions))), ("Schema", SCHEMA_VERSION)],
            ),
            self._node(
                "validation",
                "Validation",
                "Gates",
                "healthy" if gate_ok else "failed",
                None if not gates else gates[-1].created_at,
                "champion gates" if gate_ok else "gate evidence missing or failed",
                [
                    ("Passed", str(sum(1 for item in gates if item.passed))),
                    ("Failed", str(sum(1 for item in gates if not item.passed))),
                ],
            ),
            self._node(
                "container",
                "Container",
                "Image",
                container_status,
                None,
                container_summary,
                [("Digest", image or "unavailable")],
            ),
            self._node(
                "kubernetes",
                "Kubernetes",
                "Cluster",
                "healthy" if cluster["connected"] else "unavailable",
                None,
                cluster["reason"] or f"{len(cluster['pods'])} pods in namespace",
                [
                    ("Connected", "yes" if cluster["connected"] else "no"),
                    ("Metrics", "available" if cluster.get("metrics_server") else "unavailable"),
                ],
            ),
            self._node(
                "inference",
                "Inference",
                "Serving",
                "healthy" if self.router.stable else "failed",
                None,
                "local-router" if not self.settings.serving_stable_url else "http serving",
                [
                    ("Slot", "stable"),
                    (
                        "Version",
                        "" if self.router.stable is None else str(self.router.stable.version),
                    ),
                ],
            ),
            self._node(
                "monitoring",
                "Monitoring",
                "Metrics",
                "healthy" if recent else "idle",
                None if latest_prediction is None else latest_prediction.created_at,
                _monitoring_summary(bool(recent), latest_prediction),
                [("Requests", str(len(recent))), ("Errors", str(len(errors)))],
            ),
            self._node(
                "reliability",
                "Reliability",
                "Control",
                "degraded" if open_incidents else "healthy",
                None if not open_incidents else open_incidents[-1].opened_at,
                "open incidents" if open_incidents else "no open incidents",
                [("Open", str(len(open_incidents)))],
            ),
        ]
        return {
            "environment": self.settings.environment,
            "runtime": "http-serving" if self.settings.serving_stable_url else "local-router",
            "cluster_connected": cluster["connected"],
            "release_id": None if release is None else release.id,
            "active_release_id": None if active is None else active.id,
            "nodes": nodes,
        }

    def _node(
        self,
        identifier: str,
        label: str,
        kind: str,
        status: str,
        activity,
        summary: str,
        metrics: list[tuple[str, str]],
    ) -> dict[str, Any]:
        return {
            "id": identifier,
            "label": label,
            "kind": kind,
            "status": status,
            "activity_at": iso(activity) if isinstance(activity, datetime) else activity,
            "summary": summary,
            "metrics": [{"label": name, "value": value} for name, value in metrics],
        }

    def timeline(self, limit: int = 40) -> list[dict[str, Any]]:
        with self.session() as session:
            events = []
            for run in session.scalars(
                select(TrainingRun).order_by(TrainingRun.created_at.desc()).limit(limit)
            ).all():
                events.append(
                    {
                        "kind": "training",
                        "id": run.id,
                        "at": iso(run.created_at),
                        "title": f"Training {run.status}",
                        "detail": run.trigger,
                        "status": run.status,
                    }
                )
            for event in session.scalars(
                select(ReleaseEvent).order_by(ReleaseEvent.at.desc()).limit(limit)
            ).all():
                events.append(
                    {
                        "kind": "release",
                        "id": event.id,
                        "at": iso(event.at),
                        "title": f"{event.from_state or 'start'} → {event.to_state}",
                        "detail": event.reason,
                        "status": event.to_state,
                        "release_id": event.release_id,
                    }
                )
            for incident in session.scalars(
                select(Incident).order_by(Incident.opened_at.desc()).limit(limit)
            ).all():
                events.append(
                    {
                        "kind": "incident",
                        "id": incident.id,
                        "at": iso(incident.opened_at),
                        "title": incident.trigger,
                        "detail": incident.summary,
                        "status": incident.status,
                    }
                )
        events.sort(key=lambda item: item["at"] or "", reverse=True)
        return events[:limit]

    def summary(self) -> dict[str, Any]:
        topology = self.topology()
        with self.session() as session:
            champion = session.scalar(select(ModelVersion).where(ModelVersion.alias == "champion"))
            release = session.scalar(select(Release).order_by(Release.created_at.desc()))
            alerts = session.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(5)).all()
            open_incidents = list(
                session.scalars(
                    select(Incident).where(Incident.status.in_(["open", "unresolved"]))
                ).all()
            )
        series = self.series(5)
        requests = sum(point["requests"] for point in series["points"])
        errors = sum(point["errors"] for point in series["points"])
        p95_values = [point["p95_ms"] for point in series["points"] if point["p95_ms"] is not None]
        return {
            "model": None
            if champion is None
            else {
                "id": champion.id,
                "name": champion.name,
                "version": champion.version,
                "alias": champion.alias,
                "sha256": champion.artifact_sha256,
            },
            "release": None
            if release is None
            else {
                "id": release.id,
                "status": release.status,
                "environment": release.environment,
                "weight": release.canary_weight,
            },
            "requests_5m": requests,
            "error_rate_5m": None if requests == 0 else errors / requests,
            "p95_ms_5m": None if not p95_values else max(p95_values),
            "last_measurement_at": series["last_measurement_at"],
            "open_incidents": len(open_incidents),
            "alerts": len(alerts),
            "cluster_connected": topology["cluster_connected"],
            "runtime": topology["runtime"],
            "measurement_source": "operational_db",
        }

    def dump_run(self, run: TrainingRun) -> dict[str, Any]:
        return {
            "id": run.id,
            "status": run.status,
            "runtime": run.runtime,
            "reason": run.reason,
            "trigger": run.trigger,
            "seed": run.seed,
            "created_at": iso(run.created_at),
            "started_at": iso(run.started_at),
            "finished_at": iso(run.finished_at),
            "failure": run.failure,
            "log": run.log,
            "mlflow_run_id": run.mlflow_run_id,
            "mlflow_version": run.mlflow_version,
            "tracking_status": run.tracking_status,
            "tracking_error": run.tracking_error,
            "dataset_fingerprint": run.dataset_fingerprint,
            "git_commit": run.git_commit,
            "artifact_sha256": run.artifact_sha256,
            "model_version_id": run.model_version_id,
            "params": run.params,
            "metrics": run.metrics,
            "dependencies": run.dependencies,
            "actor": run.actor,
        }

    def dump_version(self, version: ModelVersion) -> dict[str, Any]:
        return {
            "id": version.id,
            "name": version.name,
            "version": version.version,
            "alias": version.alias,
            "approval": version.approval,
            "task": version.task,
            "framework": version.framework,
            "artifact_sha256": version.artifact_sha256,
            "artifact_bytes": version.artifact_bytes,
            "dataset_fingerprint": version.dataset_fingerprint,
            "dataset_rows": version.dataset_rows,
            "schema_version": version.schema_version,
            "git_commit": version.git_commit,
            "training_run_id": version.training_run_id,
            "mlflow_run_id": version.mlflow_run_id,
            "mlflow_version": version.mlflow_version,
            "image_digest": version.image_digest,
            "metrics": version.metrics,
            "params": version.params,
            "created_at": iso(version.created_at),
        }

    def dump_release(self, session: Session, release: Release) -> dict[str, Any]:
        events = session.scalars(
            select(ReleaseEvent)
            .where(ReleaseEvent.release_id == release.id)
            .order_by(ReleaseEvent.at.asc())
        ).all()
        bootstrap = any((event.evidence or {}).get("bootstrap") for event in events)
        version = session.get(ModelVersion, release.model_version_id)
        return {
            "id": release.id,
            "environment": release.environment,
            "model_version_id": release.model_version_id,
            "model_version": None if version is None else version.version,
            "status": release.status,
            "runtime": release.runtime,
            "image_digest": release.image_digest or "",
            "artifact_sha256": release.artifact_sha256,
            "git_commit": release.git_commit,
            "training_run_id": release.training_run_id,
            "config_version": release.config_version,
            "canary_weight": release.canary_weight,
            "previous_release_id": release.previous_release_id,
            "approval_status": release.approval_status,
            "failure": release.failure,
            "created_at": iso(release.created_at),
            "updated_at": iso(release.updated_at),
            "stages": stage_status(release.status, release.failed_stage or None)
            if not bootstrap
            else _bootstrap_stages(),
            "events": [
                {
                    "id": event.id,
                    "at": iso(event.at),
                    "actor": event.actor,
                    "from": event.from_state,
                    "to": event.to_state,
                    "reason": event.reason,
                    "evidence": event.evidence,
                }
                for event in events
            ],
            "routing": self._stats_for_release(session, release),
        }

    def dump_drift(self, run: DriftRun) -> dict[str, Any]:
        return {
            "id": run.id,
            "model_version_id": run.model_version_id,
            "result": run.result,
            "reference_rows": run.reference_rows,
            "current_rows": run.current_rows,
            "created_at": iso(run.created_at),
            "report": run.report,
        }

    def cluster_view(self) -> dict[str, Any]:
        cluster = probe_cluster()
        cluster["local_router"] = self.router.snapshot()
        cluster["measurement_note"] = (
            "Pod metrics come from the Kubernetes API when a cluster is connected. They are not estimated."
        )
        return cluster

    def logs(self, namespace: str, pod: str) -> dict[str, Any]:
        return pod_logs(namespace, pod)

    def set_faults(self, faults: dict[str, Any]) -> dict[str, Any]:
        return write_faults(self.settings.data_dir, faults)

    def record_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        alerts = payload.get("alerts") or [payload]
        stored = []
        with self.session() as session:
            for item in alerts:
                alert = Alert(
                    id=new_id("alr"),
                    name=str(
                        item.get("labels", {}).get("alertname") or item.get("name") or "alert"
                    ),
                    status=str(item.get("status") or "firing"),
                    severity=str(item.get("labels", {}).get("severity") or "warning"),
                    summary=str(
                        item.get("annotations", {}).get("summary") or item.get("summary") or ""
                    ),
                    payload=item,
                )
                session.add(alert)
                stored.append({"id": alert.id, "name": alert.name, "status": alert.status})
            session.commit()
        return {"stored": stored}

    def compare_runs(self, identifiers: list[str]) -> dict[str, Any]:
        with self.session() as session:
            runs = [session.get(TrainingRun, identifier) for identifier in identifiers]
        runs = [run for run in runs if run is not None]
        if len(runs) < 2:
            raise PlatformError("Select at least two stored training runs.")
        metric_names: set[str] = set()
        flat = []
        for run in runs:
            values = _flatten_metrics(run.metrics or {})
            metric_names.update(values)
            flat.append(values)
        metrics = []
        first = flat[0]
        for name in sorted(metric_names):
            values = {run.id: item.get(name) for run, item in zip(runs, flat, strict=True)}
            deltas = {}
            if first.get(name) is not None:
                for run, item in zip(runs, flat, strict=True):
                    if item.get(name) is not None:
                        deltas[run.id] = item[name] - first[name]
            metrics.append({"name": name, "values": values, "delta_vs_first": deltas})
        return {"runs": [self.dump_run(run) for run in runs], "metrics": metrics}

    def lineage(self, version_id: str) -> dict[str, Any]:
        with self.session() as session:
            version = session.get(ModelVersion, version_id)
            if version is None:
                raise PlatformError("Model version was not found.", 404)
            run = session.get(TrainingRun, version.training_run_id)
            releases = session.scalars(
                select(Release).where(Release.model_version_id == version.id)
            ).all()
            return {
                "version": self.dump_version(version),
                "training_run": None if run is None else self.dump_run(run),
                "dataset": {
                    "fingerprint": version.dataset_fingerprint,
                    "rows": version.dataset_rows,
                    "schema_version": version.schema_version,
                },
                "git_commit": version.git_commit,
                "image_digest": version.image_digest or "",
                "releases": [self.dump_release(session, release) for release in releases],
            }


def _flatten_metrics(metrics: dict[str, Any], prefix: str = "") -> dict[str, float]:
    flat: dict[str, float] = {}
    for key, value in metrics.items():
        name = f"{prefix}{key}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            flat[name] = float(value)
        elif isinstance(value, dict):
            flat.update(_flatten_metrics(value, f"{name}."))
    return flat


def _finite(value: Any) -> bool:
    try:
        return value is not None and float(value) == float(value)
    except (TypeError, ValueError):
        return False


def _run_status(run: TrainingRun | None) -> str:
    if run is None:
        return "idle"
    if run.status == "succeeded":
        return "healthy"
    if run.status == "failed":
        return "failed"
    if run.status in {"queued", "running"}:
        return "running"
    return "idle"


def _bootstrap_stages() -> dict[str, str]:
    return {
        "build": "complete",
        "validate": "complete",
        "package": "complete",
        "stage": "complete",
        "canary": "not_applicable",
        "verify": "complete",
        "promote": "complete",
    }


def _plain_features(features: dict[str, Any]) -> dict[str, Any]:
    plain = {}
    for name in FEATURE_NAMES:
        value = features.get(name)
        if hasattr(value, "item"):
            value = value.item()
        plain[name] = None if isinstance(value, float) and value != value else value
    return plain


def _trigger_from_reasons(reasons: list[str]) -> str:
    if "invalid_output" in reasons:
        return "invalid_output"
    if "error_rate" in reasons:
        return "error_rate"
    if "latency_absolute" in reasons or "latency_regression" in reasons:
        return "latency_regression"
    if "candidate_not_ready" in reasons or "stable_not_ready" in reasons:
        return "readiness_failure"
    return "smoke_failure"
