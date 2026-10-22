"""Small administrative CLI: create tables and users.

Usage:
    python -m app.cli init-db
    python -m app.cli create-user --email me@example.com [--admin]
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import create_db_engine, create_session_factory, init_models
from app.models import User, UserRole
from app.security import hash_password


def _session():
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    return engine, create_session_factory(engine)


def init_db() -> int:
    engine, _ = _session()
    init_models(engine)
    engine.dispose()
    print("database schema is up to date")
    return 0


def create_user(email: str, password: str, admin: bool) -> int:
    engine, factory = _session()
    init_models(engine)
    email = email.lower().strip()
    with factory() as session:
        if session.scalar(select(User).where(User.email == email)) is not None:
            print(f"user {email} already exists", file=sys.stderr)
            engine.dispose()
            return 1
        user = User(
            email=email,
            password_hash=hash_password(password),
            role=UserRole.admin if admin else UserRole.user,
        )
        session.add(user)
        session.commit()
        print(f"created {'admin' if admin else 'user'} {email}")
    engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create missing tables")

    user_parser = sub.add_parser("create-user", help="create a user")
    user_parser.add_argument("--email", required=True)
    user_parser.add_argument("--password", help="prompted for when omitted")
    user_parser.add_argument("--admin", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "init-db":
        return init_db()
    if args.command == "create-user":
        password = args.password or getpass.getpass("Password: ")
        if len(password) < 8:
            print("password must be at least 8 characters", file=sys.stderr)
            return 1
        return create_user(args.email, password, args.admin)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
