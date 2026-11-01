"""Local image identity recorded after a real Docker build.

A digest is attached to a model version only when the artifact hash inside the
image matches the stored artifact. A mismatched build stays on file and is
shown as a different image.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def image_build_path(data_dir: Path) -> Path:
    return data_dir / "image_build.json"


def read_image_build(data_dir: Path) -> dict[str, Any]:
    path = image_build_path(data_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_image_build(
    data_dir: Path, digest: str, artifact_sha256: str, repository: str
) -> dict[str, Any]:
    payload = {
        "digest": digest,
        "artifact_sha256": artifact_sha256,
        "repository": repository,
        "source": "local image id",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    image_build_path(data_dir).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
