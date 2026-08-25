"""P4 Razorpay billing acceptance tests.

Covers: plan seeding, free default on /me, cap enforcement (clients + monthly
invoices), signature-verified credit with idempotency proof, webhook raw-body
hmac, and the keyless 503 posture. All signatures are CRAFTED locally — no
network, no real Razorpay keys.
"""
import hashlib
import hmac as hmac_mod
import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ponytail: ensure tests run from repo root without PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

# isolated test DB — distinct file from test_tenancy's, set before app imports
TEST_DB_PATH = (Path(__file__).parent / "test_billing.db").as_posix()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

# never inherit real keys from the operator environment
for _k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
    os.environ.pop(_k, None)

import pytest
from fastapi.testclient import TestClient

from app.billing import seed_plans
from app.database import SessionLocal, engine
from app.main import app
from app import models

TEST_PASSWORD = "T3st-Passw0rd!"
KEY_ID = "rzp_test_XXXXXXXX"
KEY_SECRET = "test-key-secret"
WEBHOOK_SECRET = "test-webhook-secret"


def pytest_sessionfinish(session, exitstatus):
    try:
        engine.dispose()
        os.remove(TEST_DB_PATH)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def clean_db():
    models.Base.metadata.create_all(bind=engine)
    seed_plans()
    yield
    models.Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    app.state.limiter.enabled = False
    yield
    app.state.limiter.enabled = True


def _authed_client() -> tuple[TestClient, int]:
    """Register + login a fresh tenant; returns (client, user_id)."""
    c = TestClient(app)
    email = f"u-{uuid.uuid4().hex}@example.com"
    r = c.post("/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 201, r.text
    r = c.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code in (200, 204), r.text
    db = SessionLocal()
    try:
        return c, db.query(models.User).filter(models.User.email == email).first().id
    finally:
        db.close()


def _sign(message: str, secret: str) -> str:
    return hmac_mod.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _mk_order(user_id: int, order_id: str) -> None:
    db = SessionLocal()
    try:
        db.add(models.BillingPayment(
            user_id=user_id, razorpay_order_id=order_id,
            amount_rupees=499, status="created", created_at=datetime.now(),
        ))
        db.commit()
    finally:
        db.close()


def _mk_client(user_id: int, name: str) -> int:
    db = SessionLocal()
    try:
        c = models.Client(name=name, owner_id=user_id, created_at=datetime.now())
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id
    finally:
        db.close()


def _mk_month_invoices(user_id: int, n: int) -> None:
    db = SessionLocal()
    try:
        client_id = _mk_client(user_id, "CapCo")
        for i in range(n):
            db.add(models.Invoice(
                client_id=client_id, invoice_number=f"INV-{i}",
                created_at=datetime.now(),
            ))
        db.commit()
    finally:
        db.close()


# --- plans + defaults -----------------------------------------------------------


def test_plans_seeded_public():
    anon = TestClient(app)
    r = anon.get("/api/v1/billing/plans")
    assert r.status_code == 200
    plans = {p["code"]: p for p in r.json()}
    assert set(plans) == {"free", "pro"}
    assert plans["free"]["price_rupees"] == 0 and plans["free"]["invoice_cap"] == 10 \
        and plans["free"]["client_cap"] == 1
    assert plans["pro"]["price_rupees"] == 499 and plans["pro"]["invoice_cap"] is None


def test_me_reflects_free_default():
    c, uid = _authed_client()
    r = c.get("/api/v1/billing/me")
    assert r.status_code == 200
    body = r.json()
    assert body["plan_code"] == "free" and body["payments"] == []
    assert body["current_period_end"] is None


# --- cap enforcement --------------------------------------------------------------


def test_client_cap_enforced_on_free():
    c, uid = _authed_client()
    r = c.post("/api/v1/clients", json={"name": "Alpha"})
    assert r.status_code == 200, r.text
    r = c.post("/api/v1/clients", json={"name": "Beta"})
    assert r.status_code == 402, r.text
    detail = r.json()["detail"]
    assert detail["upgrade"] is True and detail["plan"] == "free"


def test_invoice_cap_enforced_on_free():
    c, uid = _authed_client()
    _mk_month_invoices(uid, 10)  # free cap reached via direct-DB seeding
    r = c.post(
        "/api/v1/upload-invoice",
        files={"file": ("inv.txt", b"not a real invoice")},
        data={"invoice_type": "sales"},
    )
    assert r.status_code == 402, r.text
    assert r.json()["detail"]["upgrade"] is True
    # gate fires before file validation — nothing consumed the upload slot
    db = SessionLocal()
    try:
        assert db.query(models.Invoice).count() == 10
    finally:
        db.close()


def test_pro_bypasses_caps():
    c, uid = _authed_client()
    _mk_order(uid, "order_proBypass")
    sig = _sign("order_proBypass|pay_pro1", KEY_SECRET)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAZORPAY_KEY_ID", KEY_ID)
        mp.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
        assert c.post("/api/v1/billing/verify", json={
            "order_id": "order_proBypass", "payment_id": "pay_pro1",
            "signature": sig,
        }).status_code == 200
    for name in ("A", "B", "C"):
        assert c.post("/api/v1/clients", json={"name": name}).status_code == 200


# --- verify: signature + idempotency ------------------------------------------------


def test_verify_extends_subscription_then_replay_is_noop():
    c, uid = _authed_client()
    _mk_order(uid, "order_test123")
    sig = _sign("order_test123|pay_test1", KEY_SECRET)

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAZORPAY_KEY_ID", KEY_ID)
        mp.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
        r1 = c.post("/api/v1/billing/verify", json={
            "order_id": "order_test123", "payment_id": "pay_test1",
            "signature": sig,
        })
        assert r1.status_code == 200, r1.text
        assert r1.json()["credited"] is True
        end1 = datetime.fromisoformat(r1.json()["current_period_end"])
        assert end1 > datetime.now()

        # replay: same payment credited twice must NOT extend again (rev #6)
        r2 = c.post("/api/v1/billing/verify", json={
            "order_id": "order_test123", "payment_id": "pay_test1",
            "signature": sig,
        })
        assert r2.status_code == 200
        assert r2.json()["credited"] is False
        assert datetime.fromisoformat(r2.json()["current_period_end"]) == end1

    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(user_id=uid).first()
        assert sub.plan_code == "pro" and sub.status == "active"
        paid = db.query(models.BillingPayment).filter_by(razorpay_payment_id="pay_test1").count()
        assert paid == 1
    finally:
        db.close()


def test_verify_bad_signature_rejected():
    c, uid = _authed_client()
    _mk_order(uid, "order_badsig")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAZORPAY_KEY_ID", KEY_ID)
        mp.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
        r = c.post("/api/v1/billing/verify", json={
            "order_id": "order_badsig", "payment_id": "pay_bad",
            "signature": "0" * 64,
        })
    assert r.status_code == 400


# --- webhook: raw-body hmac ----------------------------------------------------------


def _webhook_payload(order_id: str, payment_id: str) -> bytes:
    return json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": payment_id, "order_id": order_id, "amount": 49900, "currency": "INR",
        }}},
    }).encode()


def test_webhook_credits_and_bad_signature_is_400():
    c, uid = _authed_client()
    _mk_order(uid, "order_wh1")
    raw = _webhook_payload("order_wh1", "pay_wh1")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
        good = TestClient(app)
        r_ok = good.post(
            "/api/v1/billing/webhook",
            content=raw,
            headers={"x-razorpay-signature": _sign(raw.decode(), WEBHOOK_SECRET)},
        )
        assert r_ok.status_code == 200

        r_bad = good.post(
            "/api/v1/billing/webhook",
            content=raw,
            headers={"x-razorpay-signature": "f" * 64},
        )
        assert r_bad.status_code == 400

    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(user_id=uid).first()
        assert sub.plan_code == "pro"
        assert db.query(models.BillingPayment).filter_by(
            razorpay_payment_id="pay_wh1", status="paid").count() == 1
    finally:
        db.close()


def test_webhook_verify_race_credits_once():
    """Webhook first, then verify replay of the same payment → no double extension."""
    c, uid = _authed_client()
    _mk_order(uid, "order_race")
    raw = _webhook_payload("order_race", "pay_race")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAZORPAY_KEY_ID", KEY_ID)
        mp.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
        mp.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
        TestClient(app).post(
            "/api/v1/billing/webhook", content=raw,
            headers={"x-razorpay-signature": _sign(raw.decode(), WEBHOOK_SECRET)},
        )
        r = c.post("/api/v1/billing/verify", json={
            "order_id": "order_race", "payment_id": "pay_race",
            "signature": _sign("order_race|pay_race", KEY_SECRET),
        })
    assert r.status_code == 200 and r.json()["credited"] is False
    db = SessionLocal()
    try:
        rows = db.query(models.BillingPayment).filter_by(razorpay_payment_id="pay_race").all()
        assert len(rows) == 1 and rows[0].status == "paid"
    finally:
        db.close()


# --- keyless posture -------------------------------------------------------------------


def test_order_keyless_returns_503():
    c, uid = _authed_client()
    with pytest.MonkeyPatch.context() as mp:
        for k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
            mp.delenv(k, raising=False)
        r = c.post("/api/v1/billing/order", json={"plan_code": "pro"})
    assert r.status_code == 503
    assert "payments not configured" in json.dumps(r.json()["detail"])
    db = SessionLocal()
    try:
        assert db.query(models.BillingPayment).count() == 0
    finally:
        db.close()


def test_verify_keyless_returns_503():
    c, uid = _authed_client()
    with pytest.MonkeyPatch.context() as mp:
        for k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
            mp.delenv(k, raising=False)
        r = c.post("/api/v1/billing/verify", json={
            "order_id": "o", "payment_id": "p", "signature": "s",
        })
    assert r.status_code == 503
