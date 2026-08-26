"""Rate-limit enforcement proof (audit RATELIMIT-UNVERIFIED).

Every other suite disables the limiter; this one enables it and asserts 429
after exhausting the register/login budgets. W1: limits now live on wrapper
routes we own (slowapi decorators bound natively) — the _apply_limit
monkeypatch on fastapi-users route objects is gone.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_ratelimit.db")
os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use")
# NOTE: deliberately NOT disabling the limiter here.

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import engine

REGISTER_LIMIT = 5  # per hour per IP — keep in sync with app/users.py
LOGIN_LIMIT = 10    # per hour per IP — keep in sync with app/users.py


@pytest.fixture()
def client():
    engine.dispose()
    if os.path.exists("test_ratelimit.db"):
        os.remove("test_ratelimit.db")
    app.state.limiter.enabled = True
    with TestClient(app) as c:
        yield c
    app.state.limiter.enabled = False
    engine.dispose()
    if os.path.exists("test_ratelimit.db"):
        os.remove("test_ratelimit.db")


def test_register_rate_limited_after_budget(client):
    codes = []
    for i in range(REGISTER_LIMIT + 2):
        r = client.post(
            "/api/v1/auth/register",
            json={"email": f"rl{i}@example.com", "password": "RlPassw0rd!x"},
        )
        codes.append(r.status_code)
    assert codes[:REGISTER_LIMIT] == [201] * REGISTER_LIMIT, codes
    assert codes[REGISTER_LIMIT] == 429, codes
    assert codes[REGISTER_LIMIT + 1] == 429, codes


def test_login_rate_limited_after_budget(client):
    """W1: the login wrapper carries its own decorator; bad credentials also
    burn budget, so 10 attempts → 11th is a 429."""
    codes = []
    for _ in range(LOGIN_LIMIT + 2):
        r = client.post(
            "/api/v1/auth/login",
            data={"username": "nobody@example.com", "password": "wrong"},
        )
        codes.append(r.status_code)
    assert codes[:LOGIN_LIMIT] == [400] * LOGIN_LIMIT, codes
    assert codes[LOGIN_LIMIT] == 429, codes
