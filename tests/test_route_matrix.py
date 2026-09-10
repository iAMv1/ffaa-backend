"""Unhappy-path audit: route matrix — 401 on every protected op, 405 on wrong
methods.

The 401 sweep is driven by the live OpenAPI inventory (45 ops): public ops
(health, providers, plans, login, register, forgot, reset, webhook) are
excluded; every other operation must demand a session BEFORE doing any work.
The 405 sweep pins method mismatches per route family.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import os
os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use-0123456789abcdef0123456789abcdef")
os.environ.setdefault("FFAA_DEV", "1")
from app.database import engine
from app.main import app
from app import models
from app.billing import seed_plans

TEST_PASSWORD = "T3st-Passw0rd!"


@pytest.fixture(autouse=True)
def clean_db():
    models.Base.metadata.drop_all(bind=engine)
    models.Base.metadata.create_all(bind=engine)
    seed_plans()  # fail-closed billing gate needs the catalog (design §4)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    app.state.limiter.enabled = False
    yield
    app.state.limiter.enabled = True


def _register_and_login(c: TestClient) -> str:
    email = f"u-{uuid.uuid4().hex}@example.com"
    r = c.post("/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 201, r.text
    r = c.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code in (200, 204), r.text
    return email


# Every protected operation from the 45-op inventory, minus the 8 public ones.
# Path params use sentinel ids; body-carrying ops get '{}' (auth fires first).
PROTECTED: list[tuple[str, str]] = [
    ("PATCH", "/api/v1/auth/account"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/me"),
    ("GET", "/api/v1/bank-statements"),
    ("POST", "/api/v1/bank-statements/upload"),
    ("DELETE", "/api/v1/bank-statements/1"),
    ("GET", "/api/v1/billing/history"),
    ("GET", "/api/v1/billing/me"),
    ("POST", "/api/v1/billing/subscribe"),
    ("POST", "/api/v1/billing/subscriptions"),
    ("POST", "/api/v1/billing/subscriptions/sub_x/cancel"),
    ("POST", "/api/v1/billing/subscriptions/sub_x/pause"),
    ("POST", "/api/v1/billing/subscriptions/sub_x/resume"),
    ("POST", "/api/v1/billing/verify"),
    ("GET", "/api/v1/clients"),
    ("POST", "/api/v1/clients"),
    ("DELETE", "/api/v1/clients/1"),
    ("PUT", "/api/v1/clients/1"),
    ("GET", "/api/v1/clients/1/files/invoices/x.pdf"),
    ("GET", "/api/v1/clients/1/folders"),
    ("POST", "/api/v1/clients/1/send-reminder"),
    ("GET", "/api/v1/duplicates/flags"),
    ("POST", "/api/v1/duplicates/flags/1/resolve"),
    ("GET", "/api/v1/export-tally"),
    ("GET", "/api/v1/invoices"),
    ("DELETE", "/api/v1/invoices/1"),
    ("PUT", "/api/v1/invoices/1/approve"),
    ("POST", "/api/v1/invoices/1/duplicates/check"),
    ("PUT", "/api/v1/invoices/1/review"),
    ("POST", "/api/v1/reconcile"),
    ("GET", "/api/v1/reconciliations"),
    ("DELETE", "/api/v1/reconciliations/1"),
    ("GET", "/api/v1/reminders/history"),
    ("GET", "/api/v1/reminders/preview"),
    ("DELETE", "/api/v1/reminders/1"),
    ("POST", "/api/v1/upload-invoice"),
    ("POST", "/api/v1/upload-invoices"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_protected_ops_reject_anonymous_401(method, path):
    c = TestClient(app)  # no login
    r = c.request(method, path, json="{}" if method in ("POST", "PUT", "PATCH") else None)
    assert r.status_code == 401, f"{method} {path} → {r.status_code}: {r.text[:200]}"
    assert "detail" in r.json()


def test_public_ops_stay_public():
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    assert c.get("/api/v1/auth/providers").status_code == 200
    assert c.get("/api/v1/billing/plans").status_code == 200


# --- 405 method mismatches (verified against the live inventory) ------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/upload-invoice"),                     # POST-only
        ("GET", "/api/v1/auth/login"),                          # POST-only
        ("DELETE", "/api/v1/invoices"),                         # GET-only list
        ("PUT", "/api/v1/clients"),                             # GET/POST list
        ("POST", "/api/v1/export-tally"),                       # GET-only
        ("GET", "/api/v1/invoices/1/approve"),                  # PUT-only
        ("GET", "/api/v1/billing/webhook"),                     # POST-only
        ("DELETE", "/api/v1/billing/history"),                  # GET-only
        ("PATCH", "/api/v1/clients"),                           # no PATCH on list
        ("POST", "/api/v1/clients/1/folders"),                  # GET-only
        ("GET", "/api/v1/duplicates/flags/1/resolve"),          # POST-only
        ("DELETE", "/api/v1/upload-invoices"),                  # POST-only
    ],
)
def test_wrong_method_is_405(method, path):
    c = TestClient(app)
    _register_and_login(c)
    r = c.request(method, path, json="{}" if method in ("POST", "PUT", "PATCH") else None)
    assert r.status_code == 405, f"{method} {path} → {r.status_code}: {r.text[:200]}"
