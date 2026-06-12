from __future__ import annotations

from app.shared.helpers.crypto import (
    constant_time_compare,
    decrypt,
    encrypt,
    hmac_sign,
    hmac_verify,
    sha256_hash,
)


def test_sha256_is_deterministic_and_32_bytes() -> None:
    digest = sha256_hash("hph_live_secret")
    assert len(digest) == 32
    assert digest == sha256_hash("hph_live_secret")
    assert digest != sha256_hash("other")


def test_constant_time_compare() -> None:
    assert constant_time_compare(b"abc", b"abc")
    assert not constant_time_compare(b"abc", b"abd")


def test_encrypt_decrypt_roundtrip() -> None:
    plaintext = "provider-refresh-token-xyz"
    token = encrypt(plaintext)
    assert token != plaintext
    assert decrypt(token) == plaintext


def test_encrypt_is_nondeterministic() -> None:
    # Random nonce per call → different ciphertext for the same plaintext.
    assert encrypt("same") != encrypt("same")


def test_hmac_sign_and_verify() -> None:
    signature = hmac_sign("state-payload", "signing-secret")
    assert hmac_verify("state-payload", signature, "signing-secret")
    assert not hmac_verify("tampered", signature, "signing-secret")
    assert not hmac_verify("state-payload", signature, "wrong-secret")
