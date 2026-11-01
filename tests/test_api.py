import time

from fastapi.testclient import TestClient


def _token(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "kubeml-local"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_platform_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBEML_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KUBEML_DATASET_ROWS", "1600")
    monkeypatch.setenv("KUBEML_JWT_SECRET", "test-secret")
    monkeypatch.setenv("KUBEML_SCAN_LOOP", "0")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", (tmp_path / "mlruns").as_uri())
    from kubeml.platform.app import create_app

    with TestClient(create_app()) as client:
        observer = _token(client, "observer")
        engineer = _token(client, "engineer")
        approver = _token(client, "approver")
        admin = _token(client, "admin")

        topology = client.get("/api/v1/topology", headers=observer)
        assert topology.status_code == 200
        body = topology.json()
        assert body["runtime"] == "local-router"
        assert body["cluster_connected"] in {True, False}
        inference = next(node for node in body["nodes"] if node["id"] == "inference")
        assert inference["status"] == "healthy"
        container = next(node for node in body["nodes"] if node["id"] == "container")
        assert container["metrics"][0]["value"] == "unavailable"

        denied = client.post(
            "/api/v1/releases/rel_missing/promote", headers=observer, json={"reason": "no"}
        )
        assert denied.status_code == 403

        created = client.post(
            "/api/v1/training/jobs",
            headers=engineer,
            json={"reason": "candidate", "runtime": "local-thread"},
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        assert created.json()["status"] in {"queued", "running", "succeeded"}
        status = "queued"
        for _ in range(60):
            status = client.get(f"/api/v1/training/jobs/{job_id}", headers=engineer).json()[
                "status"
            ]
            if status in {"succeeded", "failed"}:
                break
            time.sleep(0.5)
        assert status == "succeeded"
        version_id = client.get(f"/api/v1/training/jobs/{job_id}", headers=engineer).json()[
            "model_version_id"
        ]
        detail = client.get(f"/api/v1/versions/{version_id}", headers=engineer).json()
        assert all(gate["passed"] for gate in detail["gates"])

        release = client.post(
            "/api/v1/releases",
            headers=engineer,
            json={"model_version_id": version_id, "environment": "local"},
        )
        assert release.status_code == 200, release.text
        release_id = release.json()["id"]
        deployed = client.post(
            f"/api/v1/releases/{release_id}/deploy", headers=engineer, json={"reason": "pin"}
        )
        assert deployed.status_code == 200, deployed.text
        assert deployed.json()["status"] == "canary"
        assert deployed.json()["canary_weight"] == 0
        assert deployed.json()["image_digest"] == ""

        weighted = client.post(
            f"/api/v1/releases/{release_id}/canary", headers=engineer, json={"weight": 50}
        )
        assert weighted.status_code == 200, weighted.text

        faulty = client.post("/api/v1/faults", headers=admin, json={"error_rate": 1})
        assert faulty.status_code == 200
        traffic = client.post(
            "/api/v1/traffic/generate",
            headers=engineer,
            json={"count": 80, "scenario": "reference", "label_delay_seconds": 0},
        )
        assert traffic.status_code == 200
        rolled = client.get(f"/api/v1/releases/{release_id}", headers=approver).json()
        assert rolled["status"] == "rolled_back"
        incidents = client.get("/api/v1/incidents", headers=observer).json()["incidents"]
        assert incidents
        assert incidents[0]["status"] == "resolved"

        client.post("/api/v1/faults", headers=admin, json={"error_rate": 0})
        repeat = client.post(
            f"/api/v1/releases/{release_id}/rollback",
            headers={**approver, "Idempotency-Key": "again-1"},
            json={"reason": "repeat", "trigger": "manual"},
        )
        repeat_again = client.post(
            f"/api/v1/releases/{release_id}/rollback",
            headers={**approver, "Idempotency-Key": "again-1"},
            json={"reason": "repeat", "trigger": "manual"},
        )
        assert repeat.status_code == 200
        assert repeat_again.json() == repeat.json()

        drifted = client.post(
            "/api/v1/traffic/generate",
            headers=engineer,
            json={"count": 60, "scenario": "drifted", "label_delay_seconds": 0},
        )
        assert drifted.status_code == 200
        analysis = client.post("/api/v1/drift/analyze", headers=engineer)
        assert analysis.status_code == 200, analysis.text
        assert analysis.json()["report"]["note"]
        performance = client.get("/api/v1/performance", headers=observer)
        assert performance.json()["available"] is False
        ingested = client.post(
            "/api/v1/labels/ingest", headers=engineer, json={"min_age_seconds": 0}
        )
        assert ingested.json()["ingested"] > 0
        measured = client.get("/api/v1/performance", headers=observer).json()
        assert measured["available"] is True
        assert measured["source"] == "ingested_labels"

        staging = client.post(
            "/api/v1/releases",
            headers=engineer,
            json={"model_version_id": version_id, "environment": "staging"},
        )
        if staging.status_code == 200:
            staging_id = staging.json()["id"]
            missing = client.post(
                f"/api/v1/releases/{staging_id}/deploy",
                headers=engineer,
                json={"reason": "no digest"},
            )
            assert missing.status_code == 400
            assert (
                "digest" in missing.json()["error"]
                or "approval" in missing.json()["error"]
                or "Kubernetes" in missing.json()["error"]
                or "Staging" in missing.json()["error"]
            )
