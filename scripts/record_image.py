"""Build the serving image and record its id when the baked artifact matches.

The value stored is the local Docker image id. It is not a registry digest
until the image is pushed. The platform attaches it only to model versions
whose artifact hash matches the file inside the image.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request


def run(command: list[str]) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def main() -> int:
    image = os.environ.get("KUBEML_SERVING_IMAGE", "kubeml-serving:dev")
    api = os.environ.get("KUBEML_API", "http://127.0.0.1:8095")
    subprocess.run(
        ["docker", "build", "-f", "deploy/docker/Dockerfile.serving", "-t", image, "."],
        check=True,
    )
    digest = run(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    artifact = run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            image,
            "-c",
            "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path('/opt/kubeml/model.json').read_bytes()).hexdigest())",
        ]
    )
    if not digest.startswith("sha256:"):
        print("Docker did not return an image id.", file=sys.stderr)
        return 1
    login = urllib.request.Request(
        f"{api}/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "kubeml-local"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(login, timeout=30) as response:
        token = json.loads(response.read())["token"]
    record = urllib.request.Request(
        f"{api}/api/v1/images",
        data=json.dumps(
            {"digest": digest, "artifact_sha256": artifact, "repository": image}
        ).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(record, timeout=30) as response:
        body = json.loads(response.read())
    print(
        json.dumps(
            {"digest": digest, "artifact_sha256": artifact, "matched": body["matched_versions"]},
            indent=2,
        )
    )
    if not body["matched_versions"]:
        print(
            "The image artifact does not match a registered model. The digest was stored and was not attached.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
