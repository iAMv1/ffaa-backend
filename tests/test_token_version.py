"""W1 auth hardening — token_version revocation proof (design D1).

The regression guards for M2/M3/M5:
- rotating the password via PATCH /auth/account kills every outstanding
  session cookie (old cookie → 401), and the new password logs in;
- reset tokens are stamped with the user's token_version (`tv` claim) and
  refused once anything bumps it — proven via an EMAIL change, which bumps
  tv without touching hashed_password (the stock fingerprint check alone
  could never catch that case);
- a token minted by the stock (claim-less) strategy still authenticates
  while tv=0 — the decided pre-W1 grace, no fleet-wide logout;
- a genuinely stale version is refused.
"""
import asyncio
import os
import uuid

os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use")
os.environ.setdefault("FFAA_DEV", "1")

import pytest
from fastapi.testclient import TestClient
from fastapi_users.jwt import decode_jwt

from app.database import SessionLocal, engine
from app.main import app
from app.users import (
    RESET_TOKEN_SECRET,
    JWTStrategy,
    SyncUserDatabase,
    UserManager,
    VersionedJWTStrategy,
    FFAA_SECRET,
)


TEST_PASSWORD = "testpass123"
RESET_AUDIENCE = ["fastapi-users:reset"]


@pytest.fixture(autouse=True)
def clean_db():
    from app import models
    models.Base.metadata.drop_all(bind=engine)
    models.Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    app.state.limiter.enabled = False


def _register(c: TestClient, email: str, password: str = TEST_PASSWORD):
    r = c.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text


def _login(c: TestClient, email: str, password: str = TEST_PASSWORD):
    r = c.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert r.status_code == 204, r.text


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}@example.com"


def _capture_reset_token(monkeypatch, store: dict):
    async def spy(self, user, token, request=None):
        store["token"] = token
    monkeypatch.setattr(UserManager, "on_after_forgot_password", spy)


def test_password_rotation_revokes_old_cookie():
    c = TestClient(app)
    email = _unique("rot")
    _register(c, email)
    _login(c, email)
    assert c.get("/api/v1/me").status_code == 200

    new_password = "NewPassw0rd!9"
    r = c.patch(
        "/api/v1/auth/account",
        json={"current_password": TEST_PASSWORD, "new_password": new_password},
    )
    assert r.status_code == 200, r.text

    # The SAME cookie now fails the tv check → 401.
    assert c.get("/api/v1/me").status_code == 401

    # A fresh session with the rotated password works.
    fresh = TestClient(app)
    _login(fresh, email, new_password)
    assert fresh.get("/api/v1/me").json()["email"].lower() == email.lower()


def test_email_change_bumps_version_and_kills_outstanding_reset_token(monkeypatch):
    """Unique tv proof: email change bumps token_version WITHOUT changing the
    password hash, so ONLY the tv claim can reject a pre-bump reset token."""
    c = TestClient(app)
    email = _unique("tv")
    _register(c, email)
    _login(c, email)

    captured: dict = {}
    _capture_reset_token(monkeypatch, captured)
    r = c.post("/api/v1/auth/forgot-password", json={"email": email})
    assert r.status_code == 202, r.text
    assert "token" in captured

    payload = decode_jwt(captured["token"], RESET_TOKEN_SECRET, RESET_AUDIENCE)
    assert payload["tv"] == 0

    new_email = _unique("tv2")
    r = c.patch(
        "/api/v1/auth/account",
        json={"current_password": TEST_PASSWORD, "email": new_email},
    )
    assert r.status_code == 200, r.text

    # Old cookie died with the bump...
    assert c.get("/api/v1/me").status_code == 401
    # ...and the pre-bump reset token is dead even though its signature,
    # audience, expiry and password fingerprint all still verify.
    r = c.post(
        "/api/v1/auth/reset-password",
        json={"token": captured["token"], "password": "ResetPassw0rd!7"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "RESET_PASSWORD_BAD_TOKEN"


def test_fresh_reset_token_still_consumable_after_rotation(monkeypatch):
    c = TestClient(app)
    email = _unique("rst")
    _register(c, email)

    captured: dict = {}
    _capture_reset_token(monkeypatch, captured)
    r = c.post("/api/v1/auth/forgot-password", json={"email": email})
    assert r.status_code == 202, r.text

    new_password = "ResetPassw0rd!7"
    r = c.post(
        "/api/v1/auth/reset-password",
        json={"token": captured["token"], "password": new_password},
    )
    assert r.status_code == 200, r.text

    # reset itself bumped the version → any prior login cookie would die;
    # logging in fresh with the new password proves the flow end-to-end.
    fresh = TestClient(app)
    _login(fresh, email, new_password)
    assert fresh.get("/api/v1/me").status_code == 200
    # old password no longer accepted
    stale = TestClient(app)
    r = stale.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code == 400


def test_missing_tv_claim_is_version_zero_grace():
    """Pre-W1 cookies carry no tv claim; they must keep working at tv=0."""
    c = TestClient(app)
    email = _unique("grace")
    _register(c, email)

    db = SessionLocal()
    try:
        from app.models import User as UserModel
        from sqlalchemy import func

        row = (
            db.query(UserModel)
            .filter(func.lower(UserModel.email) == email.lower())
            .first()
        )
        assert row is not None
        assert row.token_version == 0

        # Stock strategy mints a claim-less token exactly like pre-W1 code did.
        token = asyncio.run(
            JWTStrategy(secret=FFAA_SECRET, lifetime_seconds=3600).write_token(row)
        )
        manager = UserManager(SyncUserDatabase(db))
        versioned = VersionedJWTStrategy(secret=FFAA_SECRET, lifetime_seconds=3600)

        got = asyncio.run(versioned.read_token(token, manager))
        assert got is not None and got.id == row.id

        # Bump the column behind the token's back → same cookie is refused.
        row.token_version = 1
        db.commit()
        assert asyncio.run(versioned.read_token(token, manager)) is None
    finally:
        db.close()
