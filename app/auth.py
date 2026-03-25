"""
Authentication utilities for Curatio v2.

Provides password hashing, TOTP 2FA, and app pairing session management.
"""

import io
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional

import pyotp
import qrcode
import qrcode.constants
from base64 import b64encode
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from loguru import logger

from app.config import settings
from app.crypto import encrypt_token, decrypt_token
from app.models import AppPairingSession, DevicePairingSession, UserSession, User

# Password hashing context (bcrypt)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return pwd_context.verify(plain, hashed)


def generate_totp_secret() -> str:
    """Generate a new TOTP secret (base32)."""
    return pyotp.random_base32()


def get_totp_provisioning_uri(secret: str, email: str) -> str:
    """Get the provisioning URI for a TOTP authenticator app."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=settings.addon_name)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code with 1-window tolerance."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def generate_totp_qr_data_url(provisioning_uri: str) -> str:
    """Generate a QR code as a base64 data URL for the given provisioning URI."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=2,
    )
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def encrypt_totp_secret(secret: str) -> str:
    """Encrypt a TOTP secret for storage."""
    return encrypt_token(secret)


def decrypt_totp_secret(encrypted: str) -> str:
    """Decrypt a stored TOTP secret."""
    return decrypt_token(encrypted)


def create_user_session(user_id: int, db: Session) -> str:
    """Create a web login session for a user. Returns the session token."""
    token = secrets.token_hex(32)
    session = UserSession(
        token=token,
        user_id=user_id,
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db.add(session)
    db.commit()
    return token


def verify_user_session(token: str, db: Session) -> Optional[User]:
    """Verify a user session token. Returns the User or None."""
    session = db.query(UserSession).filter(UserSession.token == token).first()
    if not session:
        return None
    if datetime.utcnow() > session.expires_at:
        db.delete(session)
        db.commit()
        return None
    user = db.query(User).filter(User.id == session.user_id).first()
    return user


def _generate_short_code() -> str:
    """Generate a 6-character alphanumeric code (uppercase, no ambiguous chars)."""
    alphabet = string.ascii_uppercase + string.digits
    # Remove ambiguous characters: O, 0, I, 1, L
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "")
    alphabet = alphabet.replace("1", "").replace("L", "")
    return "".join(secrets.choice(alphabet) for _ in range(6))


def create_pairing_session(user_id: int, db: Session) -> dict:
    """
    Create a pairing session for app sign-in.

    Returns dict with token, short_code, qr_data_url, expires_at.
    """
    token = secrets.token_hex(32)

    # Generate unique short code (retry on collision)
    for _ in range(10):
        short_code = _generate_short_code()
        existing = (
            db.query(AppPairingSession)
            .filter(
                AppPairingSession.short_code == short_code,
                AppPairingSession.claimed == False,  # noqa: E712
                AppPairingSession.expires_at > datetime.utcnow(),
            )
            .first()
        )
        if not existing:
            break
    else:
        raise RuntimeError("Failed to generate unique short code")

    expires_at = datetime.utcnow() + timedelta(minutes=5)

    session = AppPairingSession(
        token=token,
        user_id=user_id,
        short_code=short_code,
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()

    pair_url = f"{settings.base_url}/auth/pair/{token}"
    qr_data_url = generate_totp_qr_data_url(pair_url)

    return {
        "token": token,
        "short_code": short_code,
        "qr_data_url": qr_data_url,
        "expires_at": expires_at.isoformat(),
    }


def claim_pairing_session(
    db: Session, *, token: Optional[str] = None, short_code: Optional[str] = None
) -> Optional[dict]:
    """
    Claim a pairing session by token or short_code.

    Returns dict with user_key and manifest_url, or None if invalid/expired/claimed.
    """
    if token:
        session = (
            db.query(AppPairingSession).filter(AppPairingSession.token == token).first()
        )
    elif short_code:
        session = (
            db.query(AppPairingSession)
            .filter(AppPairingSession.short_code == short_code.upper())
            .first()
        )
    else:
        return None

    if not session:
        return None
    if session.claimed:
        return None
    if datetime.utcnow() > session.expires_at:
        return None

    user = db.query(User).filter(User.id == session.user_id).first()
    if not user:
        return None

    session.claimed = True
    db.commit()

    logger.info(f"Pairing session claimed for user {user.id}")

    return {
        "user_key": user.user_key,
        "manifest_url": f"{settings.base_url}/{user.user_key}/manifest.json",
    }


def create_device_pairing_session(db: Session) -> dict:
    """
    Create a device-initiated pairing session (no auth required).

    The device displays the short_code on screen. An authenticated user
    claims it via /auth/device/claim to link their account.

    Returns dict with device_token, short_code, expires_at.
    """
    device_token = secrets.token_hex(32)

    # Generate unique short code (retry on collision)
    for _ in range(10):
        short_code = _generate_short_code()
        existing = (
            db.query(DevicePairingSession)
            .filter(
                DevicePairingSession.short_code == short_code,
                DevicePairingSession.claimed == False,  # noqa: E712
                DevicePairingSession.expires_at > datetime.utcnow(),
            )
            .first()
        )
        if not existing:
            break
    else:
        raise RuntimeError("Failed to generate unique short code")

    expires_at = datetime.utcnow() + timedelta(minutes=5)

    session = DevicePairingSession(
        device_token=device_token,
        short_code=short_code,
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()

    return {
        "device_token": device_token,
        "short_code": short_code,
        "expires_at": expires_at.isoformat(),
    }


def claim_device_pairing_session(db: Session, short_code: str, user: User) -> bool:
    """
    Claim a device pairing session by short_code (authenticated user).

    Links the user's account to the device session so the device can
    retrieve user_key on its next poll.

    Returns True if claimed, False if invalid/expired/already claimed.
    """
    session = (
        db.query(DevicePairingSession)
        .filter(DevicePairingSession.short_code == short_code.upper())
        .first()
    )
    if not session:
        return False
    if session.claimed:
        return False
    if datetime.utcnow() > session.expires_at:
        return False

    session.user_id = user.id
    session.claimed = True
    db.commit()

    logger.info(f"Device pairing claimed by user {user.id}")
    return True


def poll_device_pairing_session(db: Session, device_token: str) -> Optional[dict]:
    """
    Poll a device pairing session by device_token.

    Returns dict with user_key and manifest_url if claimed, or None if still pending.
    """
    session = (
        db.query(DevicePairingSession)
        .filter(DevicePairingSession.device_token == device_token)
        .first()
    )
    if not session:
        return None
    if datetime.utcnow() > session.expires_at:
        return None
    if not session.claimed or not session.user_id:
        return None

    user = db.query(User).filter(User.id == session.user_id).first()
    if not user:
        return None

    return {
        "user_key": user.user_key,
        "manifest_url": f"{settings.base_url}/{user.user_key}/manifest.json",
    }


def cleanup_expired_sessions(db: Session) -> int:
    """Remove expired pairing sessions and user sessions. Returns count deleted."""
    now = datetime.utcnow()
    count = 0

    # Expired pairing sessions
    expired_pairing = (
        db.query(AppPairingSession).filter(AppPairingSession.expires_at < now).all()
    )
    for s in expired_pairing:
        db.delete(s)
        count += 1

    # Expired device pairing sessions
    expired_device = (
        db.query(DevicePairingSession).filter(DevicePairingSession.expires_at < now).all()
    )
    for s in expired_device:
        db.delete(s)
        count += 1

    # Expired user sessions
    expired_user = db.query(UserSession).filter(UserSession.expires_at < now).all()
    for s in expired_user:
        db.delete(s)
        count += 1

    if count:
        db.commit()
        logger.info(f"Cleaned up {count} expired sessions")

    return count
