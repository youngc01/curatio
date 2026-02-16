"""
Token encryption utilities for securing OAuth tokens at rest.

Uses Fernet symmetric encryption derived from the app's SECRET_KEY.
"""

import base64
import hashlib

from cryptography.fernet import Fernet

from app.config import settings


def _get_fernet() -> Fernet:
    """Derive a Fernet key from the app's secret_key."""
    key = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token for storage."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a stored token."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
