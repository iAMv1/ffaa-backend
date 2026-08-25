"""PATCH /api/v1/auth/account — email/password change with current-password
verification (account-takeover guard)."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.database import engine
from app.main import app


TEST_PASSWORD = "testpass123"


@pytest.fixture(autouse=True)
def clean_db():
    # ponytail: recreate sqlite for each test
    from app import models
    models.Base.metadata.drop_all(bind=engine)
    models.Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    app.state.limiter.enabled = False


@pytest.fixture
def anon_client():
    return TestClient(app)


def _register_and_login(c: TestClient, email: str, password: str) -> None:
    r = c.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    r = c.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert r.status_code in (200, 204), r.text


@pytest.fixture
def client():
    c = TestClient(app)
    _register_and_login(c, f"u-{uuid.uuid4().hex}@example.com", TEST_PASSWORD)
    return c


def test_change_password(client):
    email = client.get("/api/v1/me").json()["email"]
    new_pw = "newpass456789"
    r = client.patch(
        "/api/v1/auth/account",
        json={"new_password": new_pw, "current_password": TEST_PASSWORD},
    )
    assert r.status_code == 200, r.text
    # JWT strategy: the existing cookie/token stays valid until expiry.
    assert client.get("/api/v1/me").status_code == 200
    # old password no longer logs in; new one does (fresh clients to dodge cookies)
    fresh = TestClient(app)
    r = fresh.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code == 400
    r = fresh.post("/api/v1/auth/login", data={"username": email, "password": new_pw})
    assert r.status_code in (200, 204), r.text


def test_wrong_current_password_rejected(client):
    r = client.patch(
        "/api/v1/auth/account",
        json={"new_password": "whatever123", "current_password": "wrong-password"},
    )
    assert r.status_code == 400
    assert "current" in r.json()["detail"].lower()


def test_current_password_required(client):
    # current_password is optional in the schema but mandatory for any change
    r = client.patch(
        "/api/v1/auth/account",
        json={"new_password": "whatever123", "current_password": None},
    )
    assert r.status_code == 400
    assert "current password" in r.json()["detail"].lower()
    r = client.patch("/api/v1/auth/account", json={})
    assert r.status_code == 400  # nothing to update
    assert "nothing" in r.json()["detail"].lower()


def test_email_change(client):
    old = client.get("/api/v1/me").json()["email"]
    new = f"renamed-{uuid.uuid4().hex}@example.com"
    r = client.patch(
        "/api/v1/auth/account",
        json={"email": new, "current_password": TEST_PASSWORD},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == new
    assert client.get("/api/v1/me").json()["email"] == new
    # login under the NEW email works
    fresh = TestClient(app)
    r = fresh.post("/api/v1/auth/login", data={"username": new, "password": TEST_PASSWORD})
    assert r.status_code in (200, 204), r.text


def test_email_change_requires_unique(client):
    other = TestClient(app)
    _register_and_login(other, "taken@example.com", TEST_PASSWORD)
    r = client.patch(
        "/api/v1/auth/account",
        json={"email": "taken@example.com", "current_password": TEST_PASSWORD},
    )
    assert r.status_code == 400
    assert "already" in r.json()["detail"].lower()


def test_account_requires_auth(anon_client):
    r = anon_client.patch(
        "/api/v1/auth/account",
        json={"new_password": "whatever123", "current_password": TEST_PASSWORD},
    )
    assert r.status_code == 401
