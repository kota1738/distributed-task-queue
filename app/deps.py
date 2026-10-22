"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.broker import Broker
from app.config import Settings, get_settings
from app.db import get_session
from app.models import User
from app.security import CurrentUser

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_broker(request: Request) -> Broker:
    return request.app.state.broker


BrokerDep = Annotated[Broker, Depends(get_broker)]

__all__ = [
    "BrokerDep",
    "CurrentUser",
    "SessionDep",
    "SettingsDep",
    "User",
    "get_broker",
    "get_session",
]
