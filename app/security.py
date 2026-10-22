"""Password hashing, JWT issuing and the authentication dependencies."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.models import User, UserRole

# bcrypt only considers the first 72 bytes of a password.
BCRYPT_MAX_BYTES = 72

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        raise ValueError(f"password must be at most {BCRYPT_MAX_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash in the database must not turn into a 500.
        return False


def create_access_token(
    user: User, settings: Settings, *, expires_in: int | None = None
) -> tuple[str, int]:
    """Return ``(token, ttl_seconds)`` for the given user."""
    ttl = expires_in if expires_in is not None else settings.access_token_ttl_seconds
    now = datetime.now(UTC)
    claims = {
        "sub": str(user.id),
        "role": user.role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, ttl


def decode_access_token(token: str, settings: Settings) -> dict:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Not authenticated")

    claims = decode_access_token(credentials.credentials, settings)
    try:
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _unauthorized("Invalid token subject") from exc

    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("User is inactive or no longer exists")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role is not UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required"
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == email.lower().strip()))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
