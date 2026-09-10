"""Unhappy-path audit: business rules that must fail gracefully, never 500.

Covers the approve math gate (S3), reminder send fallbacks (no email / SMTP
unconfigured), preview day-window edges, duplicate-flag resolve guards,
pagination clamps, tally empty-export, and filter-vs-error list semantics.
Every case asserts the documented contract read from the router source.
"""
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import os
os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use-0123456789abcdef0123456789abcdef")
os.environ.setdefault("FFAA_DEV", "1")
from app.database import engine, SessionLocal
from app.main import app
from app import models

TEST_PASSWORD = "T3st-Passw0rd!"


@pytest.fixture(autouse=True)
def clean_db():
    models.Base.metadata.drop_all(bind=engine)
    models.Base.metadata.create_all(bind=engine)
    from app.billing import seed_plans

    seed_plans()  # fail-closed billing gate needs the catalog (design §4)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    app.state.limiter.enabled = False
    yield
    app.state.limiter.enabled = True


def _register_and_login(c: TestClient, email: str | None = None) -> str:
    email = email or f"u-{uuid.uuid4().hex}@example.com"
    r = c.post("/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 201, r.text
    r = c.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code in (200, 204), r.text
    return email


@pytest.fixture
def client():
    c = TestClient(app)
    _register_and_login(c)
    return c


def _mk_client(c: TestClient, name: str = "Biz Co") -> int:
    r = c.post("/api/v1/clients", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_invoice_row(client_id: int, **over) -> int:
    db = SessionLocal()
    try:
        inv = models.Invoice(
            client_id=client_id,
            invoice_number=over.get("invoice_number", f"INV-{uuid.uuid4().hex[:6]}"),
            company_name="Biz Co",
            invoice_type="sales",
            status="pending",
            created_at=datetime.now(),
            **{k: v for k, v in over.items() if k != "invoice_number"},
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        return inv.id
    finally:
        db.close()


def _mk_flag(invoice_id: int, potential_id: int, status: str = "pending") -> int:
    db = SessionLocal()
    try:
        flag = models.DuplicateFlag(
            invoice_id=invoice_id,
            potential_duplicate_id=potential_id,
            similarity_score=92.0,
            matched_fields="invoice_number,amount",
            status=status,
            created_at=datetime.now(),
        )
        db.add(flag)
        db.commit()
        db.refresh(flag)
        return flag.id
    finally:
        db.close()


# --- approve math gate (S3) -----------------------------------------------------


def test_approve_blocks_broken_math_with_audit_payload(client):
    cid = _mk_client(client, "Gate Co")
    # taxable=1000, taxes=0, total=5000 → |1000 - 5000| >> tol → math_ok False
    inv_id = _mk_invoice_row(
        cid, taxable_value=1000.0, cgst=0.0, sgst=0.0, igst=0.0, total_amount=5000.0
    )
    r = client.put(f"/api/v1/invoices/{inv_id}/approve")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "math does not add up" in detail["detail"]
    assert "audit" in detail
    assert detail["audit"]["math_ok"] is False


def test_approve_skips_gate_for_unknown_breakdown(client):
    # taxable=0 (no extracted breakdown) → not gateable even though total is odd
    cid = _mk_client(client, "Skip Co")
    inv_id = _mk_invoice_row(cid, taxable_value=0.0, total_amount=5000.0)
    r = client.put(f"/api/v1/invoices/{inv_id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["approved"] is True


# --- reminder send fallbacks ----------------------------------------------------


def test_send_reminder_without_email_fails_gracefully(client):
    cid = _mk_client(client, "No Email Co")
    r = client.post(f"/api/v1/clients/{cid}/send-reminder", json={"send": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["error_message"] == "Client has no email"


def test_send_reminder_smtp_unconfigured_fails_gracefully(client, monkeypatch):
    from app import email_service

    cid = _mk_client(client, "No SMTP Co")
    assert client.put(f"/api/v1/clients/{cid}", json={"email": "dest@example.com"}).status_code == 200
    # Deterministic: neutralize any operator SMTP env (module-level constants).
    monkeypatch.setattr(email_service, "SMTP_HOST", "")
    monkeypatch.setattr(email_service, "SMTP_USER", "")
    monkeypatch.setattr(email_service, "SMTP_PASS", "")
    r = client.post(f"/api/v1/clients/{cid}/send-reminder", json={"send": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert "SMTP" in (body["error_message"] or "")


def test_send_reminder_no_send_leaves_pending(client):
    cid = _mk_client(client, "Draft Co")
    r = client.post(f"/api/v1/clients/{cid}/send-reminder", json={"send": False})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"


# --- preview day-window edges (no clamp in handler → must stay 200) -------------


@pytest.mark.parametrize("days", [0, -3, 100000])
def test_preview_day_edges_never_500(client, days):
    _mk_client(client, f"Edge {days}")
    r = client.get(f"/api/v1/reminders/preview?days={days}")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


# --- duplicate flag resolve guards ----------------------------------------------


def test_resolve_bogus_action_is_400(client):
    cid = _mk_client(client, "Flag Co")
    inv1 = _mk_invoice_row(cid)
    inv2 = _mk_invoice_row(cid)
    fid = _mk_flag(inv1, inv2)
    r = client.post(f"/api/v1/duplicates/flags/{fid}/resolve", json={"action": "bogus"})
    assert r.status_code == 400, r.text
    assert "accept or reject" in r.json()["detail"]


def test_resolve_twice_is_400(client):
    cid = _mk_client(client, "Twice Co")
    inv1 = _mk_invoice_row(cid)
    inv2 = _mk_invoice_row(cid)
    fid = _mk_flag(inv1, inv2)
    first = client.post(f"/api/v1/duplicates/flags/{fid}/resolve", json={"action": "reject"})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/v1/duplicates/flags/{fid}/resolve", json={"action": "reject"})
    assert second.status_code == 400, second.text
    assert "already resolved" in second.json()["detail"]


# --- pagination clamps (billing history) ----------------------------------------


@pytest.mark.parametrize("limit", [0, -5, 10000])
def test_billing_history_limit_clamps_never_500(client, limit):
    r = client.get(f"/api/v1/billing/history?limit={limit}")
    assert r.status_code == 200, r.text
    assert "items" in r.json() and "total" in r.json()


# --- tally empty export ----------------------------------------------------------


def test_tally_export_with_no_invoices_is_404(client):
    _mk_client(client, "Empty Tally Co")
    r = client.get("/api/v1/export-tally")
    assert r.status_code == 404, r.text
    assert "No invoices to export" in r.json()["detail"]


# --- list filter semantics (filter, not error) -----------------------------------


def test_invoices_filter_unknown_client_returns_empty(client):
    r = client.get("/api/v1/invoices?client_id=999999")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_reconcile_empty_client_is_graceful(client):
    cid = _mk_client(client, "Bare Reconcile Co")
    r = client.post(f"/api/v1/reconcile?client_id={cid}&confirm=false")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matches"] == []
    assert body["confirmed"] is False


def test_reconciliations_unknown_client_empty(client):
    r = client.get("/api/v1/reconciliations?client_id=999999")
    assert r.status_code == 200, r.text
    assert r.json() == []


# --- subscribe plan guards (keyless-safe: plan check fires first) ----------------


def test_subscribe_unknown_plan_is_400(client):
    from app.billing import seed_plans

    seed_plans()
    r = client.post("/api/v1/billing/subscribe", json={"plan_code": "gold"})
    assert r.status_code == 400, r.text
    assert "Unknown or free plan" in r.json()["detail"]


def test_subscribe_free_plan_is_400(client):
    from app.billing import seed_plans

    seed_plans()
    r = client.post("/api/v1/billing/subscribe", json={"plan_code": "free"})
    assert r.status_code == 400, r.text
    assert "Unknown or free plan" in r.json()["detail"]
