"""Billing acceptance tests — Razorpay Subscriptions flow (design §6/§7 step 7).

All SDK interactions are monkeypatched fakes; webhook fixtures are crafted
HMAC signatures over RAZORPAY_WEBHOOK_SECRET=test-webhook-secret.
"""
import hashlib
import hmac as hmac_mod
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ponytail: ensure tests run from repo root without PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

# isolated test DB — distinct file from test_tenancy's, set before app imports
TEST_DB_PATH = (Path(__file__).parent / "test_billing.db").as_posix()
os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use-0123456789abcdef0123456789abcdef")
os.environ.setdefault("FFAA_DEV", "1")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

# never inherit real keys from the operator environment
for _k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
    os.environ.pop(_k, None)

import pytest
from fastapi.testclient import TestClient

from app.billing import seed_plans  # noqa: E402
from app.billing_reconcile import run_reconcile  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

TEST_PASSWORD = "T3st-Passw0rd!"
KEY_ID = "rzp_test_XXXXXXXX"
KEY_SECRET = "test-key-secret"
WEBHOOK_SECRET = "test-webhook-secret"


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
    email = f"u-{uuid_hex()}@example.com"
    r = c.post("/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 201, r.text
    r = c.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code in (200, 204), r.text
    db = SessionLocal()
    try:
        uid = (
            db.query(models.User).filter(models.User.email == email.lower()).first().id
        )
        return c, uid
    finally:
        db.close()


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def _mk_sub(db, user_id: int, **fields) -> models.BillingSubscription:
    defaults = {
        "user_id": user_id,
        "plan_code": "pro",
        "rzp_subscription_id": f"sub_{uuid_hex()}",
        "rzp_status": "active",
        "updated_at": datetime.now(),
    }
    defaults.update(fields)
    sub = models.BillingSubscription(**defaults)
    db.add(sub)
    db.commit()
    return sub


def _sign(message: str, secret: str) -> str:
    return hmac_mod.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


# --- webhook fixture builders -------------------------------------------------------


def _sub_entity(sub_id: str = "sub_test1", status: str = "active", user_id=None,
                current_end: datetime | None = None, current_start: datetime | None = None):
    e = {"id": sub_id, "status": status, "notes": {}}
    if user_id is not None:
        e["notes"]["ffaa_user_id"] = str(user_id)
    if current_end is not None:
        e["current_end"] = int(current_end.timestamp())
    if current_start is not None:
        e["current_start"] = int(current_start.timestamp())
    return e


def _payment_entity(payment_id="pay_test1", amount=49900, order_id=None):
    e = {"id": payment_id, "amount": amount, "currency": "INR"}
    if order_id:
        e["order_id"] = order_id
    return e


def _event(event_type: str, sub_e: dict, payment_e: dict | None = None,
           event_id: str = "evt_0001") -> bytes:
    payload = {"subscription": {"entity": sub_e}}
    if payment_e is not None:
        payload["payment"] = {"entity": payment_e}
    body = {"id": event_id, "event": event_type, "payload": payload}
    return json.dumps(body).encode()


def _post_webhook(raw: bytes, secret: str = WEBHOOK_SECRET):
    c = TestClient(app)
    return c.post(
        "/api/v1/billing/webhook",
        content=raw,
        headers={"x-razorpay-signature": _sign(raw.decode(), secret)},
    )


@pytest.fixture()
def signed_webhooks(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)


# --- fake razorpay SDK ---------------------------------------------------------------


class _FakePlanRes:
    def __init__(self, store=None):
        self.store = store or {}
        self.calls = []

    def create(self, data):
        self.calls.append(data)
        return {"id": "plan_fake_pro", **data}


class _FakeCustomerRes:
    def __init__(self):
        self.calls = []

    def create(self, data):
        self.calls.append(data)
        return {"id": "cust_fake1", **data}


class _FakeSubscriptionRes:
    def __init__(self, store=None, created=None):
        self.store = store or {}
        self.created = created if created is not None else []

    def create(self, params):
        sid = f"sub_fake{len(self.created) + 1}"
        self.created.append(params)
        return {"id": sid, "status": "created", "short_url": f"https://rzp.io/i/{sid}"}

    def fetch(self, sid):
        if sid not in self.store:
            raise RuntimeError(f"no such subscription {sid}")
        return self.store[sid]

    def cancel(self, sid, data=None):
        return {"id": sid, "status": "active"}

    def pause(self, sid):
        return {"id": sid, "status": "paused"}

    def resume(self, sid):
        return {"id": sid, "status": "active"}


class _FakeInvoiceRes:
    def __init__(self, items=None):
        self.items = items or []

    def all(self, params):
        return {"items": [
            i for i in self.items
            if params.get("subscription_id") in (None, i.get("subscription_id"))
        ]}


class FakeRzpClient:
    """Crafted SDK double — no network."""

    def __init__(self, auth=None, subs=None, invoices=None):
        self.auth = auth
        self.plan = _FakePlanRes()
        self.customer = _FakeCustomerRes()
        self.subscription = _FakeSubscriptionRes(store=subs)
        self.invoice = _FakeInvoiceRes(items=invoices)


@pytest.fixture()
def fake_sdk(monkeypatch):
    holder = {}

    def factory(subs=None, invoices=None):
        client = FakeRzpClient(subs=subs, invoices=invoices)
        holder["client"] = client
        return client

    monkeypatch.setenv("RAZORPAY_KEY_ID", KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
    import razorpay

    monkeypatch.setattr(razorpay, "Client", lambda auth=None: holder["client"])
    return factory


# --- plans + defaults -----------------------------------------------------------


def test_plans_seeded_public():
    anon = TestClient(app)
    r = anon.get("/api/v1/billing/plans")
    assert r.status_code == 200
    plans = {p["code"]: p for p in r.json()}
    assert plans["pro"]["price_rupees"] == 499 and plans["pro"]["invoice_cap"] is None


def test_me_reflects_free_default():
    c, uid = _authed_client()
    body = c.get("/api/v1/billing/me").json()
    assert body["plan_code"] == "free"
    assert body["rzp_status"] is None
    assert body["grace_ends_at"] is None
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
    db = SessionLocal()
    try:
        client_row = models.Client(
            owner_id=uid, name=f"Solo-{uuid_hex()}", created_at=datetime.now(),
        )
        db.add(client_row)
        db.commit()
        cid = client_row.id
        for i in range(10):
            db.add(models.Invoice(
                client_id=cid, invoice_number=f"INV-FREE-{i}",
                invoice_type="sales", status="approved", created_at=datetime.now(),
            ))
        db.commit()
    finally:
        db.close()
    r = c.post(
        "/api/v1/upload-invoice",
        files={"file": ("inv.txt", b"not a real invoice")},
        data={"invoice_type": "sales"},
    )
    assert r.status_code == 402, r.text
    assert r.json()["detail"]["upgrade"] is True


def test_pro_bypasses_caps():
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        _mk_sub(db, uid, rzp_status="active", current_period_end=datetime.now() + timedelta(days=20))
    finally:
        db.close()
    for name in ("A", "B", "C"):
        assert c.post("/api/v1/clients", json={"name": name}).status_code == 200


def test_blocker_pending_with_future_grace_keeps_pro_access():
    """THE BLOCKER scenario (design §1): rzp_status=past_due (RZP pending) +
    grace_ends_at in the future ⇒ FULL Pro access while dunning runs."""
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        _mk_sub(db, uid, rzp_status="past_due",
                grace_ends_at=datetime.now() + timedelta(days=7),
                current_period_end=datetime.now() - timedelta(days=1))
    finally:
        db.close()
    # Pro-effective: caps don't bite
    assert c.post("/api/v1/clients", json={"name": "One"}).status_code == 200
    assert c.post("/api/v1/clients", json={"name": "Two"}).status_code == 200
    body = c.get("/api/v1/billing/me").json()
    assert body["plan_code"] == "pro"
    assert body["rzp_status"] == "past_due"
    assert body["grace_ends_at"] is not None


def test_grace_expiry_downgrades_to_free():
    """Once grace lapses (nightly job sets rzp_status='lapsed') Free caps bite."""
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        _mk_sub(db, uid, rzp_status="lapsed", grace_ends_at=None,
                current_period_end=datetime.now() - timedelta(days=8))
    finally:
        db.close()
    assert c.post("/api/v1/clients", json={"name": "One"}).status_code == 200
    assert c.post("/api/v1/clients", json={"name": "Two"}).status_code == 402
    assert c.get("/api/v1/billing/me").json()["plan_code"] == "free"


# --- webhook state machine ----------------------------------------------------------


def test_webhook_bad_signature_is_400(signed_webhooks):
    raw = _event("subscription.activated", _sub_entity(user_id=1))
    r = TestClient(app).post(
        "/api/v1/billing/webhook", content=raw,
        headers={"x-razorpay-signature": "deadbeef"},
    )
    assert r.status_code == 400


def test_webhook_keyless_returns_503():
    raw = _event("subscription.activated", _sub_entity(user_id=1))
    r = TestClient(app).post(
        "/api/v1/billing/webhook", content=raw,
        headers={"x-razorpay-signature": _sign(raw.decode(), WEBHOOK_SECRET)},
    )
    assert r.status_code == 503
    assert "payments not configured" in json.dumps(r.json()["detail"])


def test_unknown_event_acknowledged_200(signed_webhooks):
    raw = _event("payment.captured", _sub_entity())
    assert _post_webhook(raw).status_code == 200


def test_activated_credits_first_payment_and_grants_pro(signed_webhooks):
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="created", current_period_end=None)
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    before = datetime.now()
    raw = _event(
        "subscription.activated",
        _sub_entity(sub_id=sid),
        _payment_entity(payment_id="pay_first"),
        event_id="evt_act1",
    )
    assert _post_webhook(raw).status_code == 200
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(user_id=uid).first()
        assert sub.rzp_status == "active"
        assert sub.current_period_end is not None
        # first charge credits now+30d (calendar-agnostic), give or take slop
        delta = sub.current_period_end - before
        assert timedelta(days=29) < delta <= timedelta(days=31)
        pay = db.query(models.BillingPayment).filter_by(
            razorpay_payment_id="pay_first").one()
        assert pay.status == "paid"
        ev = db.query(models.BillingWebhookEvent).filter_by(event_id="evt_act1").one()
        assert ev.processed_at is not None
    finally:
        db.close()
    assert c.get("/api/v1/billing/me").json()["plan_code"] == "pro"


def test_charged_extends_period_to_rzp_current_end(signed_webhooks):
    c, uid = _authed_client()
    old_end = datetime.now() + timedelta(days=10)
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active", current_period_end=old_end)
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    new_end = datetime.now() + timedelta(days=40)
    raw = _event(
        "subscription.charged",
        _sub_entity(sub_id=sid, current_end=new_end),
        _payment_entity(payment_id="pay_renew1"),
        event_id="evt_chg1",
    )
    assert _post_webhook(raw).status_code == 200
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(user_id=uid).first()
        assert abs((sub.current_period_end - new_end).total_seconds()) < 2
    finally:
        db.close()


def test_amount_guard_underpaid_charge_never_extends(signed_webhooks):
    """BILL-WEBHOOK-AMOUNT: captured < plan price paise → underpaid, no credit."""
    c, uid = _authed_client()
    end_before = datetime.now() + timedelta(days=5)
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active", current_period_end=end_before)
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    raw = _event(
        "subscription.charged",
        _sub_entity(sub_id=sid,
                    current_end=datetime.now() + timedelta(days=35)),
        _payment_entity(payment_id="pay_short", amount=10000),  # ₹100 < ₹499
        event_id="evt_short",
    )
    assert _post_webhook(raw).status_code == 200
    db = SessionLocal()
    try:
        pay = db.query(models.BillingPayment).filter_by(
            razorpay_payment_id="pay_short").one()
        assert pay.status == "underpaid"
        sub = db.query(models.BillingSubscription).filter_by(user_id=uid).first()
        assert sub.current_period_end == end_before  # untouched
    finally:
        db.close()


def test_webhook_event_id_replay_guard(signed_webhooks):
    """Duplicate delivery of the SAME event_id → 200 no-op (design §5)."""
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="created", current_period_end=None)
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    raw = _event(
        "subscription.activated",
        _sub_entity(sub_id=sid),
        _payment_entity(payment_id="pay_once"),
        event_id="evt_dup",
    )
    assert _post_webhook(raw).status_code == 200
    assert _post_webhook(raw).status_code == 200  # replay
    db = SessionLocal()
    try:
        assert db.query(models.BillingWebhookEvent).filter_by(
            event_id="evt_dup").count() == 1
        assert db.query(models.BillingPayment).filter_by(
            razorpay_payment_id="pay_once").count() == 1
        sub = db.query(models.BillingSubscription).filter_by(user_id=uid).first()
        base = sub.current_period_end
    finally:
        db.close()
    # a third delivery must STILL not move the period
    _post_webhook(raw)
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(user_id=uid).first()
        assert sub.current_period_end == base
    finally:
        db.close()


def test_pending_sets_grace_then_halted_keeps_clock(signed_webhooks):
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active",
                      current_period_end=datetime.now() + timedelta(days=1))
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    raw = _event("subscription.pending", _sub_entity(sub_id=sid), event_id="evt_p1")
    assert _post_webhook(raw).status_code == 200
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "past_due"
        grace = sub.grace_ends_at
        assert timedelta(days=6) < grace - datetime.now() <= timedelta(days=7)
    finally:
        db.close()
    raw = _event("subscription.halted", _sub_entity(sub_id=sid), event_id="evt_h1")
    assert _post_webhook(raw).status_code == 200
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "past_due"
        assert sub.grace_ends_at == grace  # same clock, NOT pushed out
    finally:
        db.close()


def test_cancelled_keeps_pro_until_remaining_period(signed_webhooks):
    c, uid = _authed_client()
    remaining = datetime.now() + timedelta(days=12)
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active", current_period_end=remaining)
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    raw = _event(
        "subscription.cancelled",
        _sub_entity(sub_id=sid, status="cancelled", current_end=remaining),
        event_id="evt_c1",
    )
    assert _post_webhook(raw).status_code == 200
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "cancelled"
        assert abs((sub.grace_ends_at - remaining).total_seconds()) < 2
    finally:
        db.close()
    assert c.get("/api/v1/billing/me").json()["plan_code"] == "pro"


def test_paused_and_resumed_mirror(signed_webhooks):
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active",
                      grace_ends_at=datetime.now() + timedelta(days=2),
                      current_period_end=datetime.now() + timedelta(days=10))
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    assert _post_webhook(_event("subscription.paused", _sub_entity(
        sub_id=sid, status="paused"), event_id="evt_pa")).status_code == 200
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "paused"
    finally:
        db.close()
    assert _post_webhook(_event("subscription.resumed", _sub_entity(
        sub_id=sid, status="resumed"), event_id="evt_pr")).status_code == 200
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "active"
        assert sub.grace_ends_at is None  # recovery clears grace
    finally:
        db.close()


def test_completed_and_expired_terminal_states(signed_webhooks):
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active",
                      current_period_end=datetime.now() + timedelta(days=3))
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    _post_webhook(_event("subscription.completed", _sub_entity(
        sub_id=sid, status="completed"), event_id="evt_done"))
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "completed"
        sub.rzp_status = "active"
        db.commit()
    finally:
        db.close()
    _post_webhook(_event("subscription.expired", _sub_entity(
        sub_id=sid, status="expired"), event_id="evt_exp"))
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "expired"
    finally:
        db.close()
    assert c.get("/api/v1/billing/me").json()["plan_code"] == "free"


def test_authenticated_mandate_stays_free(signed_webhooks):
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="created", current_period_end=None)
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    _post_webhook(_event("subscription.authenticated", _sub_entity(
        sub_id=sid, status="authenticated"), event_id="evt_auth"))
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "authenticated"
    finally:
        db.close()
    # mandate registered ≠ entitlement: still free-tier until activated
    assert c.post("/api/v1/clients", json={"name": "X"}).status_code == 200
    assert c.post("/api/v1/clients", json={"name": "Y"}).status_code == 402


# --- disputes & refunds ----------------------------------------------------------------


def test_dispute_created_freezes_account_to_free(signed_webhooks):
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active",
                      current_period_end=datetime.now() + timedelta(days=15))
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    raw = _event(
        "payment.dispute.created",
        _sub_entity(sub_id=sid),
        _payment_entity(payment_id="pay_disp"),
        event_id="evt_d1",
    )
    assert _post_webhook(raw).status_code == 200
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "disputed"  # LOCAL_STATUS_EXTENSION
    finally:
        db.close()
    # freeze: gate treats disputed as Free even though period is live
    body = c.get("/api/v1/billing/me").json()
    assert body["plan_code"] == "free"


def test_dispute_won_restores_active(signed_webhooks):
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="disputed",
                      current_period_end=datetime.now() + timedelta(days=15))
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    raw = _event(
        "payment.dispute.won",
        _sub_entity(sub_id=sid, status="active"),
        _payment_entity(payment_id="pay_disp"),
        event_id="evt_d2",
    )
    assert _post_webhook(raw).status_code == 200
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "active"
    finally:
        db.close()
    assert c.get("/api/v1/billing/me").json()["plan_code"] == "pro"

def test_refund_clamps_period_and_marks_payment(signed_webhooks):
    c, uid = _authed_client()
    end = datetime.now() + timedelta(days=35)
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active", current_period_end=end)
        sid = sub.rzp_subscription_id
        db.add(models.BillingPayment(
            user_id=uid, razorpay_payment_id="pay_ref", amount_rupees=499,
            status="paid", created_at=datetime.now()))
        db.commit()
    finally:
        db.close()
    raw = _event(
        "refund.processed",
        _sub_entity(sub_id="sub_unknown"),
        _payment_entity(payment_id="pay_ref"),
        event_id="evt_r1",
    )
    assert _post_webhook(raw).status_code == 200
    db = SessionLocal()
    try:
        pay = db.query(models.BillingPayment).filter_by(razorpay_payment_id="pay_ref").one()
        assert pay.status == "refunded"
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        expected_start = end - timedelta(days=30)
        assert sub.current_period_end >= datetime.now() - timedelta(seconds=10)
        assert sub.current_period_end <= expected_start + timedelta(seconds=10)
    finally:
        db.close()


# --- /verify: HMAC-check-only (never grants credit) -------------------------------------


def _mk_payment(db, user_id, payment_id, status="created"):
    db.add(models.BillingPayment(
        user_id=user_id, razorpay_payment_id=payment_id, amount_rupees=499,
        status=status, created_at=datetime.now()))
    db.commit()


def test_verify_checks_hmac_marks_seen_never_credits(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="created", current_period_end=None)
        _mk_payment(db, uid, "pay_v1")
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    sig = _sign(f"pay_v1|{sid}", KEY_SECRET)
    r = c.post("/api/v1/billing/verify", json={
        "razorpay_payment_id": "pay_v1",
        "razorpay_subscription_id": sid,
        "razorpay_signature": sig,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["plan_code"] == "free"  # NO entitlement granted by /verify
    db = SessionLocal()
    try:
        pay = db.query(models.BillingPayment).filter_by(razorpay_payment_id="pay_v1").one()
        assert pay.status == "seen"
        sub = db.query(models.BillingSubscription).filter_by(user_id=uid).first()
        assert sub.rzp_status == "created" and sub.current_period_end is None
    finally:
        db.close()


def test_verify_bad_signature_rejected(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
    c, uid = _authed_client()
    r = c.post("/api/v1/billing/verify", json={
        "razorpay_payment_id": "pay_x",
        "razorpay_subscription_id": "sub_x",
        "razorpay_signature": "forged",
    })
    assert r.status_code == 400


def test_verify_keyless_returns_503():
    c, uid = _authed_client()
    r = c.post("/api/v1/billing/verify", json={
        "razorpay_payment_id": "pay_x",
        "razorpay_subscription_id": "sub_x",
        "razorpay_signature": "sig",
    })
    assert r.status_code == 503


# --- subscription endpoints --------------------------------------------------------------


def test_subscribe_creates_subscription_via_sdk(fake_sdk):
    fake_sdk()
    c, uid = _authed_client()
    r = c.post("/api/v1/billing/subscriptions", json={"plan_code": "pro"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subscription_id"].startswith("sub_fake")
    assert body["key_id"] == KEY_ID
    assert body["short_url"]
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(user_id=uid).one()
        assert sub.rzp_status == "created"
        assert sub.plan_code == "pro"
        plan = db.get(models.BillingPlan, "pro")
        assert plan.rzp_plan_id == "plan_fake_pro"  # cached, idempotent
    finally:
        db.close()
    # second subscribe reuses the live subscription instead of stacking mandates
    r2 = c.post("/api/v1/billing/subscriptions", json={"plan_code": "pro"})
    assert r2.status_code == 200
    assert r2.json()["subscription_id"] == body["subscription_id"]


def test_subscribe_keyless_returns_503():
    c, uid = _authed_client()
    r = c.post("/api/v1/billing/subscriptions", json={"plan_code": "pro"})
    assert r.status_code == 503
    assert "payments not configured" in json.dumps(r.json()["detail"])


def test_order_path_deleted_hard_cut():
    """The one-time-order loop is gone — no flag window (design §7 step 3)."""
    c, uid = _authed_client()
    r = c.post("/api/v1/billing/order", json={"plan_code": "pro"})
    assert r.status_code == 404


def test_cancel_proxy_and_grace_to_period_end(fake_sdk):
    client = fake_sdk()
    c, uid = _authed_client()
    end = datetime.now() + timedelta(days=9)
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active", current_period_end=end)
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    r = c.post(f"/api/v1/billing/subscriptions/{sid}/cancel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rzp_status"] == "active"  # cycle-end cancel keeps RZP active
    assert abs((datetime.fromisoformat(body["ends_at"]) - end).total_seconds()) < 2
    db = SessionLocal()
    try:
        sub = db.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert abs((sub.grace_ends_at - end).total_seconds()) < 2
    finally:
        db.close()


def test_pause_resume_proxies_owner_scoped(fake_sdk):
    fake_sdk()
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="active",
                      current_period_end=datetime.now() + timedelta(days=9))
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    assert c.post(f"/api/v1/billing/subscriptions/{sid}/pause").json()["rzp_status"] == "paused"
    assert c.post(f"/api/v1/billing/subscriptions/{sid}/resume").json()["rzp_status"] == "active"
    # foreign subscription id → 404 (owner-scoped)
    other = _authed_client()[0]
    assert other.post("/api/v1/billing/subscriptions/sub_other/pause").status_code == 404


def test_pause_only_when_active(fake_sdk):
    fake_sdk()
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        sub = _mk_sub(db, uid, rzp_status="cancelled",
                      current_period_end=datetime.now() + timedelta(days=2))
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    assert c.post(f"/api/v1/billing/subscriptions/{sid}/pause").status_code == 409


# --- history ------------------------------------------------------------------------------


def test_history_paged_receipts():
    c, uid = _authed_client()
    db = SessionLocal()
    try:
        for i in range(3):
            db.add(models.BillingPayment(
                user_id=uid, razorpay_payment_id=f"pay_hist{i}",
                amount_rupees=499, status="paid",
                created_at=datetime.now() - timedelta(minutes=i)))
        db.commit()
    finally:
        db.close()
    r = c.get("/api/v1/billing/history?limit=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    ids = [p["payment_id"] for p in body["items"]]
    assert ids[0] == "pay_hist0" and ids[1] == "pay_hist1"


def test_reconcile_drift_overwritten_from_rzp():
    db = SessionLocal()
    try:
        sub = _mk_sub(db, user_id=None or _mk_user(), rzp_status="authenticated",
                      updated_at=datetime.now() - timedelta(hours=25))
        uid = sub.user_id
        sid = sub.rzp_subscription_id
    finally:
        db.close()
    remote_end = datetime.now() + timedelta(days=22)
    fake = FakeRzpClient(subs={sid: {
        "id": sid, "status": "active",
        "current_end": int(remote_end.timestamp()),
    }})
    db2 = SessionLocal()
    try:
        summary = run_reconcile(db2, client=fake)
        sub2 = db2.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub2.rzp_status == "active"
        assert abs((sub2.current_period_end - remote_end).total_seconds()) < 2
        assert summary["checked"] == 1 and summary["repaired"] == 1
    finally:
        db2.close()




def _mk_user() -> int:
    db = SessionLocal()
    try:
        u = models.User(email=f"u-{uuid_hex()}@example.com",
                        hashed_password="x", is_active=True)
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def test_reconcile_no_drift_overwrite_for_local_extensions():
    """`lapsed` vs remote halted, `disputed` vs remote active: NEVER drift."""
    db = SessionLocal()
    try:
        u1, u2 = _mk_user(), _mk_user()
        s1 = _mk_sub(db, u1, rzp_status="lapsed", grace_ends_at=None,
                     updated_at=datetime.now() - timedelta(hours=25))
        s2 = _mk_sub(db, u2, rzp_status="disputed", grace_ends_at=None,
                     updated_at=datetime.now() - timedelta(hours=25))
        sid1, sid2 = s1.rzp_subscription_id, s2.rzp_subscription_id
    finally:
        db.close()
    fake = FakeRzpClient(subs={
        sid1: {"id": sid1, "status": "halted", "current_end": None},
        sid2: {"id": sid2, "status": "active",
               "current_end": int((datetime.now() + timedelta(days=9)).timestamp())},
    })
    db2 = SessionLocal()
    try:
        summary = run_reconcile(db2, client=fake)
        s1 = db2.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid1).one()
        s2 = db2.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid2).one()
        assert s1.rzp_status == "lapsed"
        assert s2.rzp_status == "disputed"
        assert summary["repaired"] == 0
    finally:
        db2.close()

def test_reconcile_credits_missed_invoices():
    """Lost webhook: RZP shows an active sub with a paid invoice we never saw."""
    db = SessionLocal()
    try:
        sub = _mk_sub(db, _mk_user(), rzp_status="authenticated",
                      updated_at=datetime.now() - timedelta(hours=25))
        uid, sid = sub.user_id, sub.rzp_subscription_id
    finally:
        db.close()
    remote_end = datetime.now() + timedelta(days=28)
    fake = FakeRzpClient(
        subs={sid: {"id": sid, "status": "active",
                    "current_end": int(remote_end.timestamp())}},
        invoices=[{"id": "inv_1", "subscription_id": sid, "status": "paid",
                   "payment_id": "pay_missed", "amount_paid": 49900}],
    )
    db2 = SessionLocal()
    try:
        summary = run_reconcile(db2, client=fake)
        pay = db2.query(models.BillingPayment).filter_by(razorpay_payment_id="pay_missed").one()
        assert pay.status == "paid"
        sub = db2.query(models.BillingSubscription).filter_by(rzp_subscription_id=sid).one()
        assert sub.rzp_status == "active"
        assert summary["repaired"] >= 1
    finally:
        db2.close()

def test_reconcile_expires_stale_created_rows():
    db = SessionLocal()
    try:
        sub = _mk_sub(db, _mk_user(), rzp_status="created", current_period_end=None,
                      updated_at=datetime.now() - timedelta(hours=25))
        uid = sub.user_id
    finally:
        db.close()
    db2 = SessionLocal()
    try:
        summary = run_reconcile(db2, client=None)  # keyless: local steps only
        sub = db2.query(models.BillingSubscription).filter_by(user_id=uid).one()
        assert sub.rzp_status == "expired"
        assert summary["expired"] == 1
    finally:
        db2.close()


def test_reconcile_lapses_past_due_beyond_grace():
    """The ONLY place `lapsed` is written (design §5 step 2)."""
    db = SessionLocal()
    try:
        sub = _mk_sub(db, _mk_user(), rzp_status="past_due",
                      grace_ends_at=datetime.now() - timedelta(hours=1),
                      updated_at=datetime.now())
        uid = sub.user_id
    finally:
        db.close()
    db2 = SessionLocal()
    try:
        summary = run_reconcile(db2, client=None)
        sub = db2.query(models.BillingSubscription).filter_by(user_id=uid).one()
        assert sub.rzp_status == "lapsed"
        assert sub.grace_ends_at is None
        assert summary["lapsed"] == 1
    finally:
        db2.close()
