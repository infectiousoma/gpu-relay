"""Unit tests for bridge/auth.py — JWT, API key generation/verification."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.auth import (
    API_KEY_PREFIX,
    create_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
    _decode_jwt,
)
from database.models import ApiKey, User


class TestPassword:
    def test_hash_and_verify(self):
        pw = "super-secret-123"
        hashed = hash_password(pw)
        assert hashed != pw
        assert verify_password(pw, hashed)

    def test_wrong_password_fails(self):
        hashed = hash_password("correct")
        assert not verify_password("wrong", hashed)

    def test_different_hashes_same_password(self):
        hashed1 = hash_password("same")
        hashed2 = hash_password("same")
        assert hashed1 != hashed2  # bcrypt salts differ


class TestJWT:
    def test_create_and_decode(self):
        user_id = str(uuid.uuid4())
        token, expires_in = create_access_token(user_id)
        assert isinstance(token, str)
        assert expires_in == 3600
        decoded = _decode_jwt(token)
        assert decoded == user_id

    def test_tampered_token_fails(self):
        user_id = str(uuid.uuid4())
        token, _ = create_access_token(user_id)
        tampered = token[:-5] + "XXXXX"
        assert _decode_jwt(tampered) is None

    def test_wrong_secret_fails(self):
        user_id = str(uuid.uuid4())
        with patch("bridge.auth.settings") as s:
            s.bridge_secret_key = "key-a"
            token, _ = create_access_token(user_id)
        # Decode with different key
        with patch("bridge.auth.settings") as s:
            s.bridge_secret_key = "key-b"
            result = _decode_jwt(token)
        assert result is None


class TestAPIKey:
    def test_generate_returns_three_parts(self):
        plaintext, key_hash, prefix = generate_api_key()
        assert plaintext.startswith(API_KEY_PREFIX)
        assert prefix == plaintext[:12]
        assert len(plaintext) > 12

    def test_hash_is_not_plaintext(self):
        plaintext, key_hash, _ = generate_api_key()
        assert key_hash != plaintext

    def test_key_unique_on_each_call(self):
        k1, _, _ = generate_api_key()
        k2, _, _ = generate_api_key()
        assert k1 != k2

    def test_prefix_matches_start(self):
        plaintext, _, prefix = generate_api_key()
        assert plaintext.startswith(prefix)


class TestGetCurrentUser:
    @pytest.mark.asyncio
    async def test_valid_jwt_returns_user(self, test_user, async_session):
        from fastapi.security import HTTPAuthorizationCredentials
        from bridge.auth import get_current_user

        token, _ = create_access_token(test_user.id)
        creds = HTTPAuthorizationCredentials(scheme="bearer", credentials=token)
        req = MagicMock()
        req.state = MagicMock()

        user = await get_current_user(req, creds, async_session)
        assert user.id == test_user.id

    @pytest.mark.asyncio
    async def test_invalid_jwt_raises_401(self, async_session):
        from fastapi import HTTPException
        from fastapi.security import HTTPAuthorizationCredentials
        from bridge.auth import get_current_user

        creds = HTTPAuthorizationCredentials(scheme="bearer", credentials="not.a.real.token")
        req = MagicMock()
        req.state = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await get_current_user(req, creds, async_session)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_api_key_returns_user(self, test_user, async_session):
        from fastapi.security import HTTPAuthorizationCredentials
        from bridge.auth import get_current_user, generate_api_key
        from database.models import ApiKey

        plaintext, key_hash, prefix = generate_api_key()
        api_key_row = ApiKey(
            user_id=test_user.id,
            key_hash=key_hash,
            key_prefix=prefix,
        )
        async_session.add(api_key_row)
        await async_session.commit()

        creds = HTTPAuthorizationCredentials(scheme="bearer", credentials=plaintext)
        req = MagicMock()
        req.state = MagicMock()

        user = await get_current_user(req, creds, async_session)
        assert user.id == test_user.id

    @pytest.mark.asyncio
    async def test_revoked_api_key_raises_401(self, test_user, async_session):
        from fastapi import HTTPException
        from fastapi.security import HTTPAuthorizationCredentials
        from bridge.auth import get_current_user, generate_api_key
        from database.models import ApiKey

        plaintext, key_hash, prefix = generate_api_key()
        api_key_row = ApiKey(
            user_id=test_user.id,
            key_hash=key_hash,
            key_prefix=prefix,
            revoked_at=datetime.now(timezone.utc),  # already revoked
        )
        async_session.add(api_key_row)
        await async_session.commit()

        creds = HTTPAuthorizationCredentials(scheme="bearer", credentials=plaintext)
        req = MagicMock()
        req.state = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await get_current_user(req, creds, async_session)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_creds_raises_401(self, async_session):
        from fastapi import HTTPException
        from bridge.auth import get_current_user

        req = MagicMock()
        with pytest.raises(HTTPException) as exc:
            await get_current_user(req, None, async_session)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_inactive_user_raises_401(self, test_user, async_session):
        from fastapi import HTTPException
        from fastapi.security import HTTPAuthorizationCredentials
        from bridge.auth import get_current_user

        test_user.is_active = False
        await async_session.commit()

        token, _ = create_access_token(test_user.id)
        creds = HTTPAuthorizationCredentials(scheme="bearer", credentials=token)
        req = MagicMock()
        req.state = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await get_current_user(req, creds, async_session)
        assert exc.value.status_code == 401
