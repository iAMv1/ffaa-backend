"""Unhappy-path audit: schema-validation gates (422) at the request boundary.

Exercises the S3 hardening live: money ge=0, rate le=100, Literal invoice_type,
GSTIN/PoS/source/hsn length caps, and ClientBase bounds — via the review
endpoint (full InvoiceCreate body) and the client endpoints. Pydantic rejects
at parse time → FastAPI 422 with the standard detail list.
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


@pytest.fixture
def client():
    c = TestClient(app)
    _register_and_login(c)
    return c


def _mk_invoice_row(client_id: int) -> int:
    db = SessionLocal()
    try:
        inv = models.Invoice(
            client_id=client_id,
            invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
            company_name="Val Co",
            invoice_type="sales",
            status="pending",
            created_at=datetime.now(),
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        return inv.id
    finally:
        db.close()


def _review_body(cid: int, **over):
    base = {
        "client_id": cid,  # InvoiceCreate requires it (review takes the full create schema)
        "invoice_number": "INV-VAL-001",
        "invoice_date": "2025-04-01",
        "company_name": "Val Co",
        "gst_rate": 18.0,
        "taxable_value": 100.0,
        "total_amount": 118.0,
        "cgst": 9.0,
        "sgst": 9.0,
        "igst": 0.0,
        "hsn_code": "9983",
        "invoice_type": "sales",
    }
    return {**base, **over}


# --- baseline sanity (the valid body must pass) ----------------------------------


def test_review_valid_body_is_accepted(client):
    cid = client.post("/api/v1/clients", json={"name": "Val Co"}).json()["id"]
    inv_id = _mk_invoice_row(cid)
    r = client.put(f"/api/v1/invoices/{inv_id}/review", json=_review_body(cid))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reviewed"


# --- money bounds (ge=0) ---------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("taxable_value", -1.0),
        ("total_amount", -1.0),
        ("cgst", -0.01),
        ("sgst", -0.01),
        ("igst", -0.01),
        ("quantity", -1.0),
        ("gst_rate", -0.5),
    ],
)
def test_review_negative_money_is_422(client, field, value):
    cid = client.post("/api/v1/clients", json={"name": "Neg Co"}).json()["id"]
    inv_id = _mk_invoice_row(cid)
    r = client.put(f"/api/v1/invoices/{inv_id}/review", json=_review_body(cid, **{field: value}))
    assert r.status_code == 422, r.text


def test_review_gst_rate_over_100_is_422(client):
    cid = client.post("/api/v1/clients", json={"name": "Rate Co"}).json()["id"]
    inv_id = _mk_invoice_row(cid)
    r = client.put(f"/api/v1/invoices/{inv_id}/review", json=_review_body(cid, gst_rate=101.0))
    assert r.status_code == 422, r.text


# --- Literal invoice_type ---------------------------------------------------------


@pytest.mark.parametrize("bad_type", ["quotation", "SALES", ""])
def test_review_bad_invoice_type_is_422(client, bad_type):
    cid = client.post("/api/v1/clients", json={"name": "Type Co"}).json()["id"]
    inv_id = _mk_invoice_row(cid)
    r = client.put(
        f"/api/v1/invoices/{inv_id}/review", json=_review_body(cid, invoice_type=bad_type)
    )
    assert r.status_code == 422, r.text


# --- string length caps -----------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("supplier_gstin", "X" * 16),   # max 15
        ("buyer_gstin", "X" * 16),      # max 15
        ("place_of_supply", "07X"),     # max 2
        ("source", "S" * 17),           # max 16
        ("hsn_code", "9" * 51),         # max 50
        ("company_name", "C" * 256),    # max 255
    ],
)
def test_review_overlength_strings_are_422(client, field, value):
    cid = client.post("/api/v1/clients", json={"name": "Len Co"}).json()["id"]
    inv_id = _mk_invoice_row(cid)
    r = client.put(f"/api/v1/invoices/{inv_id}/review", json=_review_body(cid, **{field: value}))
    assert r.status_code == 422, r.text


def test_review_free_text_item_description_passes(client):
    # item_description is deliberately unconstrained — long prose is valid.
    cid = client.post("/api/v1/clients", json={"name": "Prose Co"}).json()["id"]
    inv_id = _mk_invoice_row(cid)
    r = client.put(
        f"/api/v1/invoices/{inv_id}/review",
        json=_review_body(cid, item_description="W" * 5000),
    )
    assert r.status_code == 200, r.text


# --- 422 shape (fastapi standard detail list) -------------------------------------


def test_422_body_is_fastapi_validation_shape(client):
    cid = client.post("/api/v1/clients", json={"name": "Shape Co"}).json()["id"]
    inv_id = _mk_invoice_row(cid)
    r = client.put(f"/api/v1/invoices/{inv_id}/review", json=_review_body(cid, gst_rate=-1.0))
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


# --- client bounds -----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},                      # min_length=1
        {"name": "N" * 256},               # max 255
        {"name": "E Co", "email": "e" * 250 + "@x.com"},  # 256 > 255
        {"name": "G Co", "gst_number": "G" * 51},         # max 50
    ],
)
def test_client_bounds_are_422(client, payload):
    r = client.post("/api/v1/clients", json=payload)
    assert r.status_code == 422, r.text


def test_client_update_empty_name_is_422(client):
    cid = client.post("/api/v1/clients", json={"name": "Upd Co"}).json()["id"]
    r = client.put(f"/api/v1/clients/{cid}", json={"name": ""})
    assert r.status_code == 422, r.text
