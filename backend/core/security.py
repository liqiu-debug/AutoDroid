"""Security helpers: password hashing and JWT token management.

Configuration is environment-driven so team deployments don't ship the
repository default:

- ``AUTODROID_SECRET_KEY``: JWT signing key. When unset, a key is generated
  once and persisted to ``.jwt_secret`` at the project root (gitignored), so
  tokens survive restarts without hardcoding a secret in source.
- ``AUTODROID_TOKEN_EXPIRE_MINUTES``: access token lifetime in minutes
  (default: 30 days).
"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import jwt
from passlib.context import CryptContext

from backend.paths import project_path

logger = logging.getLogger(__name__)

_SECRET_FILE_NAME = ".jwt_secret"
_DEFAULT_TOKEN_EXPIRE_MINUTES = 60 * 24 * 30  # 30 days


def _load_or_create_secret() -> str:
    """Resolve the JWT secret: env var > persisted file > generate & persist."""
    env_secret = (os.environ.get("AUTODROID_SECRET_KEY") or "").strip()
    if env_secret:
        return env_secret

    secret_file = project_path(_SECRET_FILE_NAME)
    try:
        if secret_file.exists():
            stored = secret_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
    except OSError:
        logger.exception("Failed to read JWT secret file: %s", secret_file)

    generated = secrets.token_hex(32)
    try:
        secret_file.write_text(generated + "\n", encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        logger.info("Generated new JWT secret at %s", secret_file)
    except OSError:
        # 文件系统不可写（如只读部署）时退化为进程内密钥：服务重启后 token 失效。
        logger.exception(
            "Failed to persist JWT secret to %s; using in-memory secret",
            secret_file,
        )
    return generated


def _token_expire_minutes() -> int:
    raw = (os.environ.get("AUTODROID_TOKEN_EXPIRE_MINUTES") or "").strip()
    if not raw:
        return _DEFAULT_TOKEN_EXPIRE_MINUTES
    try:
        minutes = int(raw)
        if minutes <= 0:
            raise ValueError(raw)
        return minutes
    except ValueError:
        logger.warning(
            "Invalid AUTODROID_TOKEN_EXPIRE_MINUTES=%r; falling back to %s",
            raw,
            _DEFAULT_TOKEN_EXPIRE_MINUTES,
        )
        return _DEFAULT_TOKEN_EXPIRE_MINUTES


SECRET_KEY = _load_or_create_secret()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = _token_expire_minutes()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt
