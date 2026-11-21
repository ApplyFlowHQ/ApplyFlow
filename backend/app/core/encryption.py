"""Cryptographic helpers for secret encryption at rest, token hashing, and key derivation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


def _get_fernet() -> Fernet:
    settings = get_settings()
    raw_key = settings.mailbox_encryption_key.strip()
    if raw_key:
        try:
            return Fernet(raw_key.encode("ascii"))
        except Exception as exc:
            raise ValueError("MAILBOX_ENCRYPTION_KEY must be a valid 32-byte urlsafe base64 Fernet key") from exc
    # Deterministic fallback derived from APP_SECRET_KEY for local dev/tests when explicit key omitted.
    if settings.environment == "production":
        raise RuntimeError("MAILBOX_ENCRYPTION_KEY must be configured in production")
    digest = hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest()
    derived = base64.urlsafe_b64encode(digest)
    return Fernet(derived)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a sensitive string (OAuth token, SMTP password) using Fernet."""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted secret string."""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt stored secret; check MAILBOX_ENCRYPTION_KEY") from exc


def encrypt_json(payload: dict[str, Any]) -> str:
    """Serialize and encrypt a JSON dictionary."""
    return encrypt_secret(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def decrypt_json(ciphertext: str) -> dict[str, Any]:
    """Decrypt and parse a JSON dictionary."""
    if not ciphertext:
        return {}
    raw = decrypt_secret(ciphertext)
    return json.loads(raw)


def hash_token(raw_token: str) -> str:
    """Hash a high-entropy bearer/session/reset token using HMAC-SHA256."""
    secret = get_settings().app_secret_key.encode("utf-8")
    return hmac.new(secret, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_opaque_token(nbytes: int = 32) -> str:
    """Generate a URL-safe random token."""
    return secrets.token_urlsafe(nbytes)


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_S256) for OAuth2 PKCE."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge
