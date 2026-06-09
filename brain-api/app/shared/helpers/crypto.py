"""Hashing + symmetric encryption helpers (BACKEND_BEST_PRACTICES.md §7).

- ``sha256_hash`` produces the digest stored for refresh tokens, API keys, and
  invite/OAuth-state tokens. The raw value never touches the DB.
- ``constant_time_compare`` is the timing-safe comparison for those digests.
- ``hmac_sign`` / ``hmac_verify`` sign short-lived OAuth ``state`` values.
- ``encrypt`` / ``decrypt`` (AES-256-GCM) protect provider refresh tokens and
  webhook secrets at rest, keyed by ``settings.encryption_key``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config.settings import settings

_NONCE_BYTES = 12  # GCM standard nonce size
_KEY: bytes = bytes.fromhex(settings.encryption_key)


def sha256_hash(value: str) -> bytes:
    """Return the raw 32-byte SHA-256 digest of ``value`` (UTF-8)."""
    return hashlib.sha256(value.encode("utf-8")).digest()


def constant_time_compare(a: bytes, b: bytes) -> bool:
    """Timing-safe equality check for secrets/digests."""
    return hmac.compare_digest(a, b)


def hmac_sign(message: str, secret: str) -> str:
    """Return a urlsafe-base64 HMAC-SHA256 signature of ``message``."""
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def hmac_verify(message: str, signature: str, secret: str) -> bool:
    """Timing-safe verification of an :func:`hmac_sign` signature."""
    expected = hmac_sign(message, secret)
    return hmac.compare_digest(expected, signature)


def encrypt(plaintext: str) -> str:
    """AES-256-GCM encrypt; returns urlsafe-base64 of ``nonce || ciphertext``."""
    aes = AESGCM(_KEY)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt(token: str) -> str:
    """Reverse :func:`encrypt`. Raises ``cryptography`` errors on tampering."""
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    aes = AESGCM(_KEY)
    return aes.decrypt(nonce, ciphertext, None).decode("utf-8")
