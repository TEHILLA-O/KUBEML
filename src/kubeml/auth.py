from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from kubeml.config import Settings

ROLES = ("admin", "engineer", "approver", "observer")
PERMISSIONS = {
    "train": {"admin", "engineer"},
    "release_create": {"admin", "engineer"},
    "release_operate": {"admin", "engineer", "approver"},
    "approve": {"admin", "approver"},
    "rollback": {"admin", "approver"},
    "policy": {"admin"},
    "fault": {"admin"},
    "logs": {"admin", "engineer", "approver"},
    "read": {"admin", "engineer", "approver", "observer"},
}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"{salt.hex()}:{derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split(":", 1)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 120_000
    )
    return derived.hex() == digest_hex


def issue_token(settings: Settings, user: Any) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.username,
        "uid": user.id,
        "role": user.role,
        "name": user.name,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.token_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def read_token(settings: Settings, token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


def allowed(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, set())
