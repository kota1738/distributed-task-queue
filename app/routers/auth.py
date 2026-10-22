"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.deps import CurrentUser, SessionDep, SettingsDep
from app.schemas import TokenRequest, TokenResponse, UserResponse
from app.security import authenticate, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token", response_model=TokenResponse)
def issue_token(body: TokenRequest, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    """Exchange an email and password for a short-lived bearer token."""
    user = authenticate(session, body.email, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, ttl = create_access_token(user, settings)
    return TokenResponse(access_token=token, expires_in=ttl)


@router.get("/me", response_model=UserResponse)
def read_me(user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(user)
