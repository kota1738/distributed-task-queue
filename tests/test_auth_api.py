from __future__ import annotations

from tests.conftest import ADMIN, ALICE, PASSWORD


def test_token_is_issued_for_valid_credentials(client, users):
    response = client.post("/api/v1/auth/token", json={"email": ALICE, "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 600
    assert body["access_token"]


def test_email_is_case_insensitive(client, users):
    response = client.post(
        "/api/v1/auth/token", json={"email": "ALICE@Example.com", "password": PASSWORD}
    )
    assert response.status_code == 200


def test_wrong_password_is_rejected(client, users):
    response = client.post("/api/v1/auth/token", json={"email": ALICE, "password": "nope"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_unknown_user_is_rejected(client, users):
    response = client.post(
        "/api/v1/auth/token", json={"email": "ghost@example.com", "password": PASSWORD}
    )
    assert response.status_code == 401


def test_me_returns_the_authenticated_user(client, alice_headers, users):
    response = client.get("/api/v1/auth/me", headers=alice_headers)
    assert response.status_code == 200
    assert response.json()["email"] == ALICE
    assert response.json()["role"] == "user"


def test_me_requires_a_token(client, users):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_me_rejects_a_malformed_token(client, users):
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_deactivated_user_cannot_use_an_existing_token(
    client, session_factory, alice_headers, users
):
    from sqlalchemy import select

    from app.models import User

    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == ALICE))
        user.is_active = False
        session.commit()

    response = client.get("/api/v1/auth/me", headers=alice_headers)
    assert response.status_code == 401


def test_admin_role_is_exposed(client, admin_headers):
    response = client.get("/api/v1/auth/me", headers=admin_headers)
    assert response.json()["role"] == "admin"
    assert response.json()["email"] == ADMIN
