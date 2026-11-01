"""Release state machine.

Transitions are explicit. A release cannot skip validation, and a rollback is a
separate decision from an MLflow alias change.
"""

from __future__ import annotations

from typing import Any

STATES = (
    "candidate",
    "validated",
    "awaiting_approval",
    "deploying",
    "canary",
    "healthy",
    "promoted",
    "failed",
    "rolled_back",
    "cancelled",
)

ALLOWED: dict[str, set[str]] = {
    "candidate": {"validated", "failed", "cancelled"},
    "validated": {"awaiting_approval", "deploying", "cancelled"},
    "awaiting_approval": {"deploying", "cancelled"},
    "deploying": {"canary", "failed"},
    "canary": {"healthy", "failed", "rolled_back"},
    "healthy": {"promoted", "failed", "rolled_back"},
    "promoted": {"rolled_back", "failed"},
    "failed": {"rolled_back", "cancelled"},
    "rolled_back": set(),
    "cancelled": set(),
}

STAGES = ("build", "validate", "package", "stage", "canary", "verify", "promote")


class TransitionError(ValueError):
    pass


def assert_transition(source: str, target: str) -> None:
    if source not in ALLOWED:
        raise TransitionError(f"Unknown release state {source}.")
    if target not in ALLOWED[source]:
        raise TransitionError(f"Cannot move a release from {source} to {target}.")


def stage_status(state: str, failed_stage: str | None = None) -> dict[str, str]:
    """Map a release state onto the release-studio track.

    ``current`` is the stage that is actually in progress. Failed and cancelled
    releases keep the last stage that did not finish.
    """
    current_by_state = {
        "candidate": "validate",
        "validated": "package",
        "awaiting_approval": "stage",
        "deploying": "stage",
        "canary": "canary",
        "healthy": "verify",
        "promoted": None,
        "failed": failed_stage or "canary",
        "rolled_back": "promote",
        "cancelled": failed_stage or "validate",
    }
    current = current_by_state[state]
    result: dict[str, str] = {}
    seen_current = current is None
    for stage in STAGES:
        if state == "promoted":
            result[stage] = "complete"
            continue
        if state == "rolled_back" and stage == "promote":
            result[stage] = "rolled_back"
            continue
        if state == "rolled_back":
            result[stage] = "complete"
            continue
        if not seen_current and stage == current:
            result[stage] = "failed" if state in {"failed", "cancelled"} else "current"
            seen_current = True
            continue
        result[stage] = "pending" if seen_current else "complete"
    return result


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return float(ordered[index])


def summarise_routes(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {}
    for slot in ("stable", "candidate"):
        rows = [event for event in events if event["slot"] == slot]
        latencies = [
            float(event["latency_ms"]) for event in rows if event.get("latency_ms") is not None
        ]
        errors = [event for event in rows if event.get("status") != "ok"]
        invalid = [event for event in rows if event.get("error_type") == "invalid_output"]
        summary[slot] = {
            "requests": len(rows),
            "errors": len(errors),
            "error_rate": (len(errors) / len(rows)) if rows else None,
            "invalid_outputs": len(invalid),
            "p50_ms": percentile(latencies, 0.50),
            "p95_ms": percentile(latencies, 0.95),
            "p99_ms": percentile(latencies, 0.99),
        }
    return summary


def evaluate_canary(
    stats: dict[str, Any], policy: dict[str, Any], stable_ready: bool, candidate_ready: bool
) -> dict[str, Any]:
    rules = policy["canary"]
    candidate = stats["candidate"]
    stable = stats["stable"]
    reasons = []
    if not candidate_ready:
        return {"decision": "not_ready", "reasons": ["candidate_not_ready"], "sufficient": False}
    if not stable_ready:
        reasons.append("stable_not_ready")
    if candidate["requests"] < int(rules["min_requests"]):
        return {
            "decision": "insufficient_evidence",
            "reasons": ["min_requests"],
            "sufficient": False,
            "requests": candidate["requests"],
            "required": int(rules["min_requests"]),
        }
    if candidate["error_rate"] is not None and candidate["error_rate"] > float(
        rules["max_error_rate"]
    ):
        reasons.append("error_rate")
    p95_seconds = None if candidate["p95_ms"] is None else candidate["p95_ms"] / 1000
    if p95_seconds is not None and p95_seconds > float(rules["max_p95_latency_seconds"]):
        reasons.append("latency_absolute")
    if (
        candidate["p95_ms"] is not None
        and stable["p95_ms"] not in (None, 0)
        and candidate["p95_ms"] > stable["p95_ms"] * float(rules["max_latency_regression_ratio"])
    ):
        reasons.append("latency_regression")
    if candidate["invalid_outputs"]:
        reasons.append("invalid_output")
    return {
        "decision": "regressing" if reasons else "passing",
        "reasons": reasons,
        "sufficient": True,
        "candidate": candidate,
        "stable": stable,
    }


def rollback_authorised(
    trigger: str, evidence: dict[str, Any], policy: dict[str, Any], manual: bool
) -> dict[str, Any]:
    rules = policy["rollback"]
    if trigger == "feature_drift":
        return {
            "authorised": False,
            "manual": manual,
            "reason": "Feature drift is an investigation signal. Policy forbids automatic model rollback for it.",
        }
    if manual:
        return {
            "authorised": True,
            "manual": True,
            "reason": "An authorised operator requested rollback.",
        }
    flag = {
        "readiness_failure": "on_readiness_failure",
        "error_rate": "on_error_rate",
        "latency_regression": "on_latency_regression",
        "invalid_output": "on_invalid_output",
        "smoke_failure": "on_smoke_failure",
        "rollout_timeout": "on_rollout_timeout",
        "crash_loop": "on_crash_loop",
    }.get(trigger)
    if flag is None or not rules.get(flag, False):
        return {
            "authorised": False,
            "manual": False,
            "reason": f"Trigger {trigger} is not enabled in policy.",
        }
    requests = int(evidence.get("requests") or 0)
    if trigger in {"error_rate", "latency_regression", "invalid_output"} and requests < int(
        policy["canary"]["min_requests"]
    ):
        return {
            "authorised": False,
            "manual": False,
            "reason": "Not enough measured requests to justify an automated rollback.",
        }
    return {
        "authorised": True,
        "manual": False,
        "reason": f"Policy authorises rollback on {trigger}.",
    }
