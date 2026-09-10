"""Unhappy-path audit: tenant scoping — other users' objects are 404, never
403, and never leak into list/filter responses; plus malformed/unknown id
shapes. The app's standing invariant (404-not-403, no enumeration) is pinned
across every object route.
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


def _mk_client(c: TestClient, name: str) -> int:
    r = c.post("/api/v1/clients", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_invoice_row(client_id: int, number: str) -> int:
    db = SessionLocal()
    try:
        inv = models.Invoice(
            client_id=client_id, invoice_number=number, company_name="A Co",
            invoice_type="sales", status="pending", created_at=datetime.now(),
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        return inv.id
    finally:
        db.close()


def _mk_bank_row(client_id: int, narration: str) -> int:
    db = SessionLocal()
    try:
        bs = models.BankStatement(
            client_id=client_id, date=datetime.now().date(), narration=narration,
            debit=100.0, credit=0.0, balance=0.0, created_at=datetime.now(),
        )
        db.add(bs)
        db.commit()
        db.refresh(bs)
        return bs.id
    finally:
        db.close()


def _mk_flag(invoice_id: int, potential_id: int) -> int:
    db = SessionLocal()
    try:
        flag = models.DuplicateFlag(
            invoice_id=invoice_id, potential_duplicate_id=potential_id,
            similarity_score=90.0, matched_fields="number", status="pending",
            created_at=datetime.now(),
        )
        db.add(flag)
        db.commit()
        db.refresh(flag)
        return flag.id
    finally:
        db.close()


@pytest.fixture
def two_users():
    """(alice_client, bob_client, a_objects) — A owns data; B probes it."""
    alice = TestClient(app)
    _register_and_login(alice)
    a_client = _mk_client(alice, "Alice Trade Co")
    a_inv1 = _mk_invoice_row(a_client, "INV-A-1")
    a_inv2 = _mk_invoice_row(a_client, "INV-A-2")
    a_bank = _mk_bank_row(a_client, "NEFT ALICE")
    a_flag = _mk_flag(a_inv1, a_inv2)

    bob = TestClient(app)
    _register_and_login(bob)
    return alice, bob, {
        "client": a_client, "inv1": a_inv1, "inv2": a_inv2,
        "bank": a_bank, "flag": a_flag,
    }


# --- lists must not leak other tenants' rows ---------------------------------------


def test_lists_do_not_leak_foreign_rows(two_users):
    alice, bob, a = two_users
    assert all(i["id"] != a["inv1"] for i in bob.get("/api/v1/invoices").json())
    assert all(c["id"] != a["client"] for c in bob.get("/api/v1/clients").json())
    assert all(b["id"] != a["bank"] for b in bob.get("/api/v1/bank-statements").json())
    assert all(f["id"] != a["flag"] for f in bob.get("/api/v1/duplicates/flags").json())
    assert all(i["id"] != a["inv1"] for i in bob.get(f"/api/v1/invoices?client_id={a['client']}").json())


# --- object routes: foreign ids are 404, never 403 ---------------------------------


def test_foreign_client_object_routes_404(two_users):
    _, bob, a = two_users
    # No GET /clients/{id} surface exists (list/PUT/DELETE only) — a GET on
    # that path is a 405, pinned in test_route_matrix. Ownership probes use
    # the mutating routes instead.
    assert bob.put(f"/api/v1/clients/{a['client']}", json={"name": "Hijack"}).status_code == 404
    assert bob.delete(f"/api/v1/clients/{a['client']}").status_code == 404
    assert bob.get(f"/api/v1/clients/{a['client']}/folders").status_code == 404
    assert bob.get(f"/api/v1/clients/{a['client']}/files/invoices/nope.pdf").status_code == 404
    assert bob.post(f"/api/v1/clients/{a['client']}/send-reminder", json={}).status_code == 404


def test_foreign_invoice_routes_404(two_users):
    _, bob, a = two_users
    assert bob.put(f"/api/v1/invoices/{a['inv1']}/review", json={
        "client_id": a["client"], "invoice_number": "X", "invoice_type": "sales",
    }).status_code == 404
    assert bob.put(f"/api/v1/invoices/{a['inv1']}/approve").status_code == 404
    assert bob.delete(f"/api/v1/invoices/{a['inv1']}").status_code == 404
    assert bob.post(f"/api/v1/invoices/{a['inv1']}/duplicates/check").status_code == 404


def test_foreign_bank_recon_tally_404(two_users):
    _, bob, a = two_users
    assert bob.delete(f"/api/v1/bank-statements/{a['bank']}").status_code == 404
    assert bob.post(f"/api/v1/reconcile?client_id={a['client']}&confirm=false").status_code == 404
    assert bob.get(f"/api/v1/export-tally?client_id={a['client']}").status_code == 404
    assert bob.post(
        f"/api/v1/duplicates/flags/{a['flag']}/resolve", json={"action": "reject"}
    ).status_code == 404


def test_foreign_flag_never_mutates(two_users):
    alice, bob, a = two_users
    bob.post(f"/api/v1/duplicates/flags/{a['flag']}/resolve", json={"action": "accept"})
    # Alice's flag must be untouched by the failed foreign attempt.
    flags = {f["id"]: f["status"] for f in alice.get("/api/v1/duplicates/flags").json()}
    assert flags[a["flag"]] == "pending"


# --- id shapes ---------------------------------------------------------------------
# NOTE: GET /invoices/{id} was removed as an orphan surface — malformed path
# params are probed with methods that actually exist.


def test_malformed_ids_are_422(two_users):
    _, bob, a = two_users
    assert bob.delete("/api/v1/invoices/abc").status_code == 422
    assert bob.post("/api/v1/invoices/1.5/duplicates/check").status_code == 422


def test_unknown_and_negative_ids_are_404(two_users):
    _, bob, a = two_users
    assert bob.delete("/api/v1/invoices/-5").status_code == 404
    assert bob.delete("/api/v1/clients/0").status_code == 404
    assert bob.delete("/api/v1/invoices/999999").status_code == 404
    assert bob.delete("/api/v1/reminders/999999").status_code == 404
