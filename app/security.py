import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.config import settings


def encrypt_legacy_password(password: str) -> str:
    """
    Menghasilkan password yang kompatibel dengan fungsi
    encrypt_password() pada ERP PHP lama.
    """

    # PHP hash("sha256", $secret_key) menghasilkan hexadecimal string.
    key_hex = hashlib.sha256(
        settings.legacy_password_key.encode("utf-8")
    ).hexdigest()

    iv_hex = hashlib.sha256(
        settings.legacy_password_iv.encode("utf-8")
    ).hexdigest()

    # AES-256 membutuhkan 32 byte.
    # OpenSSL/PHP menggunakan 32 karakter awal dari key string.
    key = key_hex[:32].encode("utf-8")

    # AES-CBC membutuhkan IV sepanjang 16 byte.
    iv = iv_hex[:16].encode("utf-8")

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded_password = (
        padder.update(password.encode("utf-8"))
        + padder.finalize()
    )

    cipher = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
    )

    encryptor = cipher.encryptor()

    encrypted = (
        encryptor.update(padded_password)
        + encryptor.finalize()
    )

    # openssl_encrypt() dengan options=0 mengembalikan Base64.
    openssl_result = base64.b64encode(encrypted)

    # PHP kembali menjalankan base64_encode().
    double_base64 = base64.b64encode(openssl_result).decode("utf-8")

    # Sama dengan str_replace('=', '', $output).
    return double_base64.replace("=", "")


def verify_legacy_password(
    plain_password: str,
    stored_password: str,
) -> bool:
    if not stored_password:
        return False

    encrypted_input = encrypt_legacy_password(
        plain_password.strip()
    )

    return hmac.compare_digest(
        encrypted_input,
        stored_password,
    )


def create_access_token(
    subject: str,
    additional_claims: dict[str, Any] | None = None,
    expires_in_seconds: int | None = None,
) -> tuple[str, int]:
    if expires_in_seconds is None:
        expires_in_seconds = settings.jwt_expire_minutes * 60
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=expires_in_seconds
    )

    payload: dict[str, Any] = {
        "sub": subject,
        "iat": datetime.now(timezone.utc),
        "exp": expires_at,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": secrets.token_hex(16),
        "type": "access",
    }

    if additional_claims:
        payload.update(additional_claims)

    token = jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    return token, expires_in_seconds


def create_refresh_token() -> tuple[str, int]:
    expires_in_seconds = (
        settings.refresh_token_expire_days * 24 * 60 * 60
    )
    return secrets.token_urlsafe(48), expires_in_seconds


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )

        if payload.get("type") != "access":
            return None

        if not payload.get("sub"):
            return None

        return payload

    except jwt.ExpiredSignatureError:
        return None

    except jwt.InvalidTokenError:
        return None
