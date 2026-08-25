"""P1 multi-tenant acceptance tests:
  1. cross-tenant isolation — user B cannot read/update/delete user A's client
  2. OCR auto-mint stamps owner_id from the authenticated user
  3. unauthenticated requests are rejected with 401
"""
import os
import sys
import uuid
from pathlib import Path

# ponytail: ensure tests run from repo root without PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

# same isolated test DB as test_smoke.py — set before app imports
TEST_DB_PATH = (Path(__file__).parent / "test_ffaa.db").as_posix()
os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use")
os.environ.setdefault("FFAA_DEV", "1")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal, engine
from app.main import app
from app import models

TEST_PASSWORD = "T3st-Passw0rd!"


def pytest_sessionfinish(session, exitstatus):
    try:
        engine.dispose()
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def clean_db():
    models.Base.metadata.create_all(bind=engine)
    yield
    models.Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    app.state.limiter.enabled = False
    yield
    app.state.limiter.enabled = True


def _authed_client() -> tuple[TestClient, str]:
    """Register + login a fresh tenant; returns (client, email)."""
    c = TestClient(app)
    email = f"u-{uuid.uuid4().hex}@example.com"
    r = c.post("/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 201, r.text
    r = c.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code in (200, 204), r.text
    return c, email


def _user_id(email: str) -> int:
    db = SessionLocal()
    try:
        u = db.query(models.User).filter(models.User.email == email).first()
        assert u is not None
        return u.id
    finally:
        db.close()


# --- 1. cross-tenant isolation ----------------------------------------------------


def test_cross_tenant_isolation():
    user_a, _ = _authed_client()
    user_b, _ = _authed_client()

    r = user_a.post("/api/v1/clients", json={"name": "A Tenant Co"})
    assert r.status_code in (200, 204), r.text
    cid = r.json()["id"]

    # read: B's list never contains A's client...
    b_listed = [c["id"] for c in user_b.get("/api/v1/clients").json()]
    assert cid not in b_listed
    # ...and B's scoped reads return empty
    assert user_b.get(f"/api/v1/clients/{cid}/folders").status_code == 404

    # update
    assert user_b.put(f"/api/v1/clients/{cid}", json={"email": "pwn@evil.com"}).status_code == 404

    # delete
    assert user_b.delete(f"/api/v1/clients/{cid}").status_code == 404

    # transitive surfaces stay isolated too (invoices list is owner-scoped)
    assert user_b.get("/api/v1/invoices").json() == []

    # A still sees and can update its own client
    assert any(c["id"] == cid for c in user_a.get("/api/v1/clients").json())
    assert user_a.put(f"/api/v1/clients/{cid}", json={"email": "ok@a.com"}).status_code == 200


# --- 2. auto-mint stamps owner -----------------------------------------------------


def test_automint_stamps_owner():
    c, email = _authed_client()

    def fake_ocr(path):
        return {
            "invoice_number": "INV-AUTO-1",
            "invoice_date": "15/03/2026",
            "company_name": "Auto Mint Textiles Pvt Ltd",
            "gst_rate": 18.0,
            "taxable_value": 1000.0,
            "total_amount": 1180.0,
            "cgst": 90.0,
            "sgst": 90.0,
            "igst": 0.0,
        }

    from app.routers import invoices as invoices_router
    monkey = pytest.MonkeyPatch()
    monkey.setattr(invoices_router, "process_invoice_document", fake_ocr)
    try:
        r = c.post(
            "/api/v1/upload-invoice",
            files={"file": ("inv.jpg", b"fake image bytes", "image/jpeg")},
            data={"invoice_type": "sales"},  # no client_id → auto-mint path
        )
    finally:
        monkey.undo()
    assert r.status_code in (200, 204), r.text
    cid = r.json()["client_id"]

    db = SessionLocal()
    try:
        row = db.query(models.Client).filter(models.Client.id == cid).first()
        assert row is not None
        assert row.auto_created is True
        assert row.owner_id == _user_id(email), "auto-minted client must belong to uploader"
    finally:
        db.close()


# --- 3. unauthenticated access ------------------------------------------------------

def test_unauthenticated_requests_rejected():
    anon = TestClient(app)  # no login
    assert anon.get("/api/v1/clients").status_code == 401
    assert anon.post("/api/v1/clients", json={"name": "x"}).status_code == 401
    assert anon.delete("/api/v1/clients/1").status_code == 401
    assert anon.get("/api/v1/invoices").status_code == 401
