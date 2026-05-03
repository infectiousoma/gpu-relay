"""Authentication: JWT tokens and API key verification.

Two credential paths:
  - Bearer JWT  — issued by POST /auth/login, short-lived (1h)
  - Bearer API key — long-lived, hashed in DB (api_keys table)

Both resolve to a User row injected as a FastAPI dependency.
"""

from __future__ import annotations

import hashlib
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridge.settings import settings
from database.models import ApiKey, User
from database.session import get_session

log = structlog.get_logger(__name__)

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer = HTTPBearer(auto_error=False)

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
API_KEY_PREFIX = "sk-llm-"
API_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

_BCRYPT_MAX = 72


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX:
        encoded = encoded[:_BCRYPT_MAX]
    return _pwd_ctx.hash(encoded)


def verify_password(plain: str, hashed: str) -> bool:
    encoded = plain.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX:
        encoded = encoded[:_BCRYPT_MAX]
    return _pwd_ctx.verify(encoded, hashed)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(user_id: str) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    expires_in = ACCESS_TOKEN_EXPIRE_MINUTES * 60
    expire = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    payload = {"sub": user_id, "exp": expire}
    token = jwt.encode(payload, settings.bridge_secret_key, algorithm=ALGORITHM)
    return token, expires_in


def _decode_jwt(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.bridge_secret_key, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# API key generation + hashing
# ---------------------------------------------------------------------------

def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext_key, key_hash, key_prefix)."""
    alphabet = string.ascii_letters + string.digits
    secret = "".join(secrets.choice(alphabet) for _ in range(API_KEY_BYTES))
    plaintext = API_KEY_PREFIX + secret
    prefix = plaintext[:12]
    key_hash = _hash_api_key(plaintext)
    return plaintext, key_hash, prefix


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def hash_api_key(key: str) -> str:
    return _hash_api_key(key)


async def _lookup_api_key(token: str, session: AsyncSession) -> User | None:
    """Scan active API keys; bcrypt-verify to find the matching user."""
    result = await session.execute(
        select(ApiKey)
        .join(User)
        .where(
            ApiKey.revoked_at.is_(None),
            User.is_active.is_(True),
            ApiKey.key_prefix == token[:12],
        )
    )
    rows = result.scalars().all()
    token_hash = _hash_api_key(token)
    for key_row in rows:
        if key_row.key_hash == token_hash:
            key_row.last_used_at = datetime.now(timezone.utc)
            user = await session.get(User, key_row.user_id)
            return user
    return None


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def get_current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing credentials")

    token = creds.credentials

    # --- API key path ---
    if token.startswith(API_KEY_PREFIX):
        user = await _lookup_api_key(token, session)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        request.state.auth_method = "api_key"
        return user

    # --- JWT path ---
    user_id = _decode_jwt(token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    request.state.auth_method = "jwt"
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    from database.models import UserRole
    if user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
