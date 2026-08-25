"""Reminders endpoint characterization tests.

Mirrors test_smoke.py conventions (isolated sqlite DB + TestClient).
Pins CURRENT behavior: preview window math, pending on send=false,
failed status for missing email, SMTP failures recorded not raised.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ponytail: ensure tests run from repo root without PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

# same isolated test DB as test_smoke.py — set before app imports
TEST_DB_PATH = (Path(__file__).parent / "test_ffaa.db").as_posix()
os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use")
os.environ.setdefault("FFAA_DEV", "1")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

import uuid

import pytest
from fastapi.testclient import TestClient

from app import models
from app.database import SessionLocal, engine
from app.main import app


def pytest_sessionfinish(session, exitstatus):
    try:
        if TEST_DB_PATH and os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def clean_db():
    # ponytail: recreate sqlite for each test
    models.Base.metadata.create_all(bind=engine)
    yield
    models.Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    app.state.limiter.enabled = False
    yield
    app.state.limiter.enabled = True


TEST_PASSWORD = "T3st-Passw0rd!"
_current_owner = [None]  # set per-test by the authed `client` fixture


@pytest.fixture
def client():
    """Authenticated TestClient for one fresh tenant."""
    c = TestClient(app)
    email = f"u-{uuid.uuid4().hex}@example.com"
    r = c.post("/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 201, r.text
    r = c.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code in (200, 204), r.text
    db = SessionLocal()
    try:
        u = db.query(models.User).filter(models.User.email == email).first()
        assert u is not None
        _current_owner[0] = u.id
    finally:
        db.close()
    yield c
    _current_owner[0] = None


def _make_client(name, email=None):
    # owned by the fixture's tenant so API reads can see it
    db = SessionLocal()
    try:
        c = models.Client(
            name=name, email=email, owner_id=_current_owner[0],
            created_at=datetime.now(),
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id
    finally:
        db.close()


def _add_invoice(client_id, days_ago=0):
    db = SessionLocal()
    try:
        inv = models.Invoice(
            client_id=client_id,
            invoice_number=f"INV-{client_id}",
            invoice_type="sales",
            total_amount=100.0,
            created_at=datetime.now() - timedelta(days=days_ago),
        )
        db.add(inv)
        db.commit()
    finally:
        db.close()


def _add_bank(client_id, days_ago=0):
    db = SessionLocal()
    try:
        b = models.BankStatement(
            client_id=client_id,
            narration="deposit",
            debit=0.0,
            credit=50.0,
            balance=50.0,
            created_at=datetime.now() - timedelta(days=days_ago),
        )
        db.add(b)
        db.commit()
    finally:
        db.close()


# --- GET /api/v1/reminders/preview ----------------------------------------------


def test_preview_lists_only_clients_with_missing_docs(client):
    a = _make_client("No Docs Co", "a@x.com")
    b = _make_client("Fully Documented Co", "b@x.com")
    _add_invoice(b)
    _add_bank(b)

    r = client.get("/api/v1/reminders/preview")
    assert r.status_code == 200
    entries = r.json()
    assert [e["client_id"] for e in entries] == [a]
    entry = entries[0]
    assert entry["missing_docs"] == ["Sales/Purchase invoices", "Bank statements"]
    assert entry["last_upload"] is None
    assert entry["days_since_upload"] is None
def test_preview_window_math_excludes_recent_docs(client):
    # both clients have fresh BANK docs; only the invoice window differs
    c_old = _make_client("Old Uploader")
    c_new = _make_client("Recent Uploader")
    _add_invoice(c_old, days_ago=60)  # outside the default 30-day window
    _add_invoice(c_new, days_ago=5)   # inside
    _add_bank(c_old)
    _add_bank(c_new)

    # default window: old invoice is stale -> only that client is flagged,
    # and only for invoices
    entries_default = client.get("/api/v1/reminders/preview").json()
    assert [e["client_id"] for e in entries_default] == [c_old]
    assert entries_default[0]["missing_docs"] == ["Sales/Purchase invoices"]

    # widen the window: invoice counts as recent -> nobody flagged
    entries_wide = client.get("/api/v1/reminders/preview?days=90").json()
    assert entries_wide == []


def test_preview_last_upload_from_latest_doc(client):
    cid = _make_client("Last Upload Co")
    # no invoices -> client is flagged, but the fresh bank upload anchors
    # last_upload / days_since_upload
    _add_bank(cid, days_ago=2)
    entry = client.get("/api/v1/reminders/preview").json()[0]
    assert entry["last_upload"] is not None
    assert entry["days_since_upload"] == 2




# --- POST /api/v1/clients/{id}/send-reminder -------------------------------------


def test_send_false_leaves_status_pending(client):
    cid = _make_client("Pending Co", "p@x.com")
    r = client.post(f"/api/v1/clients/{cid}/send-reminder", json={"send": False})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert data["error_message"] is None
    assert data["sent_at"] is None
    # recorded in history too
    hist = client.get("/api/v1/reminders/history").json()
    assert len(hist) == 1
    assert hist[0]["status"] == "pending"


def test_no_email_and_send_true_marks_failed(client):
    cid = _make_client("No Email Co")  # no email set
    r = client.post(f"/api/v1/clients/{cid}/send-reminder", json={"send": True})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "failed"
    assert data["error_message"] == "Client has no email"
    assert data["sent_at"] is None


def test_smtp_failure_recorded_not_raised(client, monkeypatch):
    cid = _make_client("Broken SMTP Co", "x@y.com")

    def boom(to, subject, body):
        raise RuntimeError("SMTP connection refused")

    monkeypatch.setattr("app.services.send_email", boom)
    r = client.post(f"/api/v1/clients/{cid}/send-reminder", json={"send": True})
    assert r.status_code == 200  # no 500 despite SMTP failure
    data = r.json()
    assert data["status"] == "failed"
    assert "SMTP connection refused" in data["error_message"]


def test_successful_send_marks_sent(client, monkeypatch):
    cid = _make_client("Happy Path Co", "h@x.com")
    sent = {}

    def fake_send(to, subject, body):
        sent["to"] = to
        sent["body"] = body

    monkeypatch.setattr("app.services.send_email", fake_send)
    r = client.post(f"/api/v1/clients/{cid}/send-reminder", json={"send": True})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "sent"
    assert data["sent_at"] is not None
    assert sent["to"] == "h@x.com"


def test_custom_message_overrides_body(client):
    cid = _make_client("Custom Msg Co", "c@x.com")
    r = client.post(
        f"/api/v1/clients/{cid}/send-reminder",
        json={"send": False, "custom_message": "Please send the March docs."},
    )
    assert r.status_code == 200
    assert r.json()["body"] == "Please send the March docs."


def test_send_reminder_unknown_client_404(client):
    r = client.post("/api/v1/clients/99999/send-reminder", json={"send": False})
    assert r.status_code == 404
