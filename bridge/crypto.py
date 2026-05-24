"""Fernet encryption for user-supplied provider API keys stored in DB."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from bridge.settings import settings


def _fernet() -> Fernet:
    # Derive a 32-byte key from the secret via SHA-256 then base64url-encode
    raw = hashlib.sha256(settings.provider_key_secret.encode()).digest()
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


def encrypt_provider_key(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_provider_key(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
