from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.models import User, UserRole
from app.security import create_access_token, decode_access_token, hash_password, verify_password


@pytest.fixture
def settings() -> Settings:
    return Settings(jwt_secret="unit-test-secret", access_token_ttl_seconds=60)


def test_password_hash_roundtrip():
    hashed = hash_password("a-reasonably-long-password")
    assert hashed != "a-reasonably-long-password"
    assert verify_password("a-reasonably-long-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_password_longer_than_bcrypt_limit_is_rejected():
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("x" * 73)


def test_verify_password_with_malformed_hash_returns_false():
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_token_roundtrip(settings: Settings):
    user = User(id=7, email="person@example.com", role=UserRole.admin)

    token, ttl = create_access_token(user, settings)

    claims = decode_access_token(token, settings)
    assert claims["sub"] == "7"
    assert claims["role"] == "admin"
    assert ttl == 60


def test_expired_token_is_rejected(settings: Settings):
    user = User(id=1, email="person@example.com", role=UserRole.user)
    token, _ = create_access_token(user, settings, expires_in=-5)

    with pytest.raises(HTTPException) as excinfo:
        decode_access_token(token, settings)
    assert excinfo.value.status_code == 401
    assert "expired" in excinfo.value.detail.lower()


def test_token_signed_with_another_secret_is_rejected(settings: Settings):
    other = Settings(jwt_secret="a-different-secret")
    user = User(id=1, email="person@example.com", role=UserRole.user)
    token, _ = create_access_token(user, other)

    with pytest.raises(HTTPException) as excinfo:
        decode_access_token(token, settings)
    assert excinfo.value.status_code == 401
