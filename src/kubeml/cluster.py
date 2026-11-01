from __future__ import annotations

import re
import subprocess
import time
from threading import Event, Lock
from typing import Any

_SECRET = re.compile(r"(?i)(password|token|secret|authorization|api[_-]?key)\s*[=:]\s*\S+")
_PROBE_TTL_SECONDS = 12.0
_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_probe_waiters: dict[str, Event] = {}
_probe_lock = Lock()


def redact(text: str) -> str:
    return _SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", text)


def probe_cluster(namespace: str = "kubeml") -> dict[str, Any]:
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(namespace)
        if cached is not None and now - cached[0] < _PROBE_TTL_SECONDS:
            return cached[1]
        waiter = _probe_waiters.get(namespace)
        if waiter is None:
            waiter = Event()
            _probe_waiters[namespace] = waiter
            leader = True
        else:
            leader = False
    if not leader:
        waiter.wait(timeout=15)
        with _probe_lock:
            cached = _probe_cache.get(namespace)
        if cached is not None:
            return cached[1]
        return _unavailable("The cluster probe did not finish.")
    try:
        result = _probe_cluster(namespace)
    except Exception as exc:
        result = _unavailable(f"Cluster probe failed: {exc}")
    with _probe_lock:
        _probe_cache[namespace] = (time.monotonic(), result)
        _probe_waiters.pop(namespace, None)
    waiter.set()
    return result


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "connected": False,
        "mode": "unavailable",
        "reason": reason,
        "pods": [],
        "deployments": [],
        "jobs": [],
        "metrics_server": False,
    }


def _probe_cluster(namespace: str) -> dict[str, Any]:
    try:
        client = subprocess.run(
            ["kubectl", "version", "--client", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except FileNotFoundError:
        return {
            "connected": False,
            "mode": "unavailable",
            "reason": "kubectl is not installed. The local router is the runtime that is actually serving.",
            "pods": [],
            "deployments": [],
            "jobs": [],
            "metrics_server": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "connected": False,
            "mode": "unavailable",
            "reason": "kubectl timed out.",
            "pods": [],
            "deployments": [],
            "jobs": [],
            "metrics_server": False,
        }
    if client.returncode != 0:
        return {
            "connected": False,
            "mode": "unavailable",
            "reason": redact(client.stderr.strip() or "kubectl is installed but could not start."),
            "pods": [],
            "deployments": [],
            "jobs": [],
            "metrics_server": False,
        }
    cluster = subprocess.run(
        ["kubectl", "get", "pods", "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if cluster.returncode != 0:
        return {
            "connected": False,
            "mode": "unavailable",
            "reason": redact(cluster.stderr.strip() or "No reachable cluster context."),
            "kubectl_client": True,
            "pods": [],
            "deployments": [],
            "jobs": [],
            "metrics_server": False,
        }
    import json

    payload = json.loads(cluster.stdout or '{"items": []}')
    pods = [_pod(item) for item in payload.get("items", [])]
    deployments = _json("deployments", namespace)
    jobs = _json("jobs", namespace)
    metrics = subprocess.run(
        ["kubectl", "get", "apiservice", "v1beta1.metrics.k8s.io", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    metrics_ok = False
    if metrics.returncode == 0:
        body = json.loads(metrics.stdout or "{}")
        conditions = body.get("status", {}).get("conditions", [])
        metrics_ok = any(
            item.get("type") == "Available" and item.get("status") == "True" for item in conditions
        )
    return {
        "connected": True,
        "mode": "kubernetes",
        "namespace": namespace,
        "reason": "",
        "pods": pods,
        "deployments": deployments,
        "jobs": jobs,
        "metrics_server": metrics_ok,
        "autoscaler_note": (
            "metrics-server is available, so CPU-based HPA can observe utilisation."
            if metrics_ok
            else "metrics-server is not available. Replica counts can still be set, but HPA is not observing CPU."
        ),
    }


def pod_logs(namespace: str, pod: str, tail: int = 200) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace):
        raise ValueError("Invalid namespace.")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", pod):
        raise ValueError("Invalid pod name.")
    result = subprocess.run(
        ["kubectl", "logs", "-n", namespace, pod, f"--tail={min(tail, 400)}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return {"available": False, "log": redact(result.stderr.strip())}
    return {"available": True, "log": redact(result.stdout)}


def _json(kind: str, namespace: str) -> list[dict[str, Any]]:
    import json

    result = subprocess.run(
        ["kubectl", "get", kind, "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode != 0:
        return []
    payload = json.loads(result.stdout or '{"items": []}')
    if kind == "deployments":
        return [_deployment(item) for item in payload.get("items", [])]
    return [_job(item) for item in payload.get("items", [])]


def _pod(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("status", {})
    spec = item.get("spec", {})
    containers = spec.get("containers", [])
    container_status = status.get("containerStatuses") or [{}]
    first = container_status[0] if container_status else {}
    return {
        "name": item.get("metadata", {}).get("name"),
        "namespace": item.get("metadata", {}).get("namespace"),
        "phase": status.get("phase"),
        "ready": bool((first.get("ready"))),
        "restarts": int(first.get("restartCount") or 0),
        "image": containers[0].get("image") if containers else "",
        "node": spec.get("nodeName") or "",
    }


def _deployment(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("status", {})
    spec = item.get("spec", {})
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    container = containers[0] if containers else {}
    return {
        "name": item.get("metadata", {}).get("name"),
        "desired": spec.get("replicas"),
        "ready": status.get("readyReplicas") or 0,
        "available": status.get("availableReplicas") or 0,
        "image": container.get("image", ""),
        "requests": container.get("resources", {}).get("requests", {}),
        "limits": container.get("resources", {}).get("limits", {}),
    }


def _job(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("status", {})
    return {
        "name": item.get("metadata", {}).get("name"),
        "succeeded": status.get("succeeded") or 0,
        "failed": status.get("failed") or 0,
        "active": status.get("active") or 0,
    }
