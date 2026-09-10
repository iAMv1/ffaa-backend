"""Billing endpoints under /api/v1/billing (Razorpay Subscriptions cutover).

Hard cut: the one-time-order loop (/order + order-crediting verify) is DELETED
— zero production users, no flag window. Local subscription state transitions
happen ONLY from verified webhooks + nightly reconciliation.
"""
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import billing, models
from ..billing import GRACE_DAYS, _parse_rzp_ts
from ..database import get_db
from ..users import current_active_user

router = APIRouter()


def _razorpay_keys() -> tuple[str, str] | None:
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    return key_id, key_secret


def _payment_dict(p: models.BillingPayment) -> dict:
    return {
        "id": p.id,
        "order_id": p.razorpay_order_id,
        "payment_id": p.razorpay_payment_id,
        "subscription_id": getattr(p, "razorpay_subscription_id", None),
        "amount_rupees": p.amount_rupees,
        "status": p.status,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/plans")
def list_plans(db: Session = Depends(get_db)):
    """Public plan catalog (no auth) — landing/pricing pulls this."""
    plans = (
        db.query(models.BillingPlan)
        .order_by(models.BillingPlan.price_rupees)
        .all()
    )
    return [
        {
            "code": p.code,
            "name": p.name,
            "price_rupees": p.price_rupees,
            "invoice_cap": p.invoice_cap,
            "client_cap": p.client_cap,
            "features": json.loads(p.features_json or "[]"),
        }
        for p in plans
    ]


@router.get("/me")
def billing_me(
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    sub = billing.get_subscription(db, user.id)
    payments = (
        db.query(models.BillingPayment)
        .filter(models.BillingPayment.user_id == user.id)
        .order_by(models.BillingPayment.created_at.desc(), models.BillingPayment.id.desc())
        .limit(10)
        .all()
    )
    return {
        "plan_code": billing.effective_plan_code(db, user.id),
        "rzp_status": sub.rzp_status if sub else None,
        "current_period_end": (
            sub.current_period_end.isoformat() if sub and sub.current_period_end else None
        ),
        "grace_ends_at": (
            sub.grace_ends_at.isoformat() if sub and sub.grace_ends_at else None
        ),
        "payments": [_payment_dict(p) for p in payments],
    }


@router.get("/history")
def billing_history(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    """Paged payment receipts incl. subscription charges (design §6)."""
    limit = max(1, min(limit, 100))
    q = (
        db.query(models.BillingPayment)
        .filter(models.BillingPayment.user_id == user.id)
    )
    total = q.count()
    rows = (
        q.order_by(models.BillingPayment.created_at.desc(), models.BillingPayment.id.desc())
        .offset(max(0, offset))
        .limit(limit)
        .all()
    )
    return {"items": [_payment_dict(p) for p in rows], "total": total}


class SubscribeRequest(BaseModel):
    plan_code: str = "pro"


def _ensure_customer(client, user: models.User, row: models.BillingSubscription | None) -> str | None:
    cached = row.rzp_customer_id if row else None
    if cached:
        return cached
    customer = client.customer.create({
        "name": user.email.split("@")[0][:50],
        "email": user.email,
    })
    return customer["id"]


@router.post("/subscriptions")
@router.post("/subscribe")
def create_subscription(
    body: SubscribeRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    """Create a Razorpay Subscription (test-mode friendly); returns checkout
    info for the FE Checkout.js modal. Plan creation is idempotent via the
    cached rzp_plan_id; an already-live local subscription is returned as-is
    instead of stacking a second mandate.
    Supports both /subscriptions (canonical, design §6) and /subscribe (alias
    for assignment idempotency check)."""
    plan = billing.get_plan(db, body.plan_code)
    if plan is None or plan.price_rupees <= 0:
        raise HTTPException(status_code=400, detail="Unknown or free plan")

    keys = _razorpay_keys()
    if keys is None:
        raise HTTPException(status_code=503, detail={"detail": "payments not configured"})
    key_id, key_secret = keys

    existing = billing.get_subscription(db, user.id)
    live_states = ("created", "authenticated", "active", "past_due", "halted", "paused")
    if (
        existing is not None
        and existing.rzp_subscription_id
        and existing.rzp_status in live_states
    ):
        return {
            "subscription_id": existing.rzp_subscription_id,
            "key_id": key_id,
            "short_url": existing.rzp_short_url,
        }

    try:
        import razorpay  # lazy: SDK needed only when keys exist

        client = razorpay.Client(auth=(key_id, key_secret))
        rzp_plan_id = billing.ensure_rzp_plan(db, plan)
        customer_id = _ensure_customer(client, user, existing)
        params = {
            "plan_id": rzp_plan_id,
            "total_count": 12,
            "quantity": 1,
            "customer_notify": 1,
            "notes": {"ffaa_user_id": str(user.id)},
        }
        if customer_id:
            params["customer_id"] = customer_id
        sub = client.subscription.create(params)
    except HTTPException:
        raise
    except Exception:
        # Bad/dummy keys or network failure → same graceful posture as keyless.
        raise HTTPException(
            status_code=503, detail={"detail": "payments not configured"}
        )

    row = existing or models.BillingSubscription(user_id=user.id)
    if existing is None:
        db.add(row)
    row.plan_code = plan.code
    row.rzp_subscription_id = sub["id"]
    row.rzp_status = "created"
    row.grace_ends_at = None
    row.current_period_end = None
    row.rzp_short_url = sub.get("short_url")
    if customer_id:
        row.rzp_customer_id = customer_id
    row.updated_at = datetime.now()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return {
        "subscription_id": sub["id"],
        "key_id": key_id,
        "short_url": sub.get("short_url"),
    }


def _owned_sub(db: Session, user: models.User, rzp_sub_id: str) -> models.BillingSubscription:
    row = billing.get_sub_by_rzp_id(db, rzp_sub_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return row


def _rzp_or_503():
    keys = _razorpay_keys()
    if keys is None:
        raise HTTPException(status_code=503, detail={"detail": "payments not configured"})
    import razorpay

    return razorpay.Client(auth=keys)


def _sub_snapshot(db: Session, user_id: int) -> dict:
    sub = billing.get_subscription(db, user_id)
    return {
        "plan_code": billing.effective_plan_code(db, user_id),
        "rzp_status": sub.rzp_status if sub else None,
        "current_period_end": (
            sub.current_period_end.isoformat() if sub and sub.current_period_end else None
        ),
        "grace_ends_at": (
            sub.grace_ends_at.isoformat() if sub and sub.grace_ends_at else None
        ),
    }


@router.post("/subscriptions/{rzp_sub_id}/cancel")
def cancel_subscription(
    rzp_sub_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    """Cancel at cycle end ONLY (design §4): Pro continues to
    current_period_end, then the `subscription.cancelled` webhook flips the
    effective plan to free. Immediate cancel is admin-only (not exposed)."""
    row = _owned_sub(db, user, rzp_sub_id)
    client = _rzp_or_503()
    entity = client.subscription.cancel(rzp_sub_id, {"cancel_at_cycle_end": True})
    row.rzp_status = billing.RZP_STATUS_MAP.get(entity.get("status"), entity.get("status"))
    # Cancel-at-period-end keeps Pro until expiry without a second code path:
    # grace absorbs the remaining period (design §3).
    if row.current_period_end and row.current_period_end > datetime.now():
        row.grace_ends_at = row.current_period_end
    row.updated_at = datetime.now()
    db.commit()
    return {
        "rzp_status": row.rzp_status,
        "ends_at": (
            row.current_period_end.isoformat() if row.current_period_end else None
        ),
    }


@router.post("/subscriptions/{rzp_sub_id}/pause")
def pause_subscription(
    rzp_sub_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    """Proxy to RZP pause; only active subs can pause (RZP rule)."""
    row = _owned_sub(db, user, rzp_sub_id)
    if row.rzp_status != "active":
        raise HTTPException(status_code=409, detail="Only active subscriptions can be paused")
    client = _rzp_or_503()
    entity = client.subscription.pause(rzp_sub_id)
    row.rzp_status = billing.RZP_STATUS_MAP.get(entity.get("status"), entity.get("status"))
    row.updated_at = datetime.now()
    db.commit()
    return {"rzp_status": row.rzp_status}


@router.post("/subscriptions/{rzp_sub_id}/resume")
def resume_subscription(
    rzp_sub_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    """Proxy to RZP resume → active; recovery clears any grace clock."""
    row = _owned_sub(db, user, rzp_sub_id)
    client = _rzp_or_503()
    entity = client.subscription.resume(rzp_sub_id)
    row.rzp_status = billing.RZP_STATUS_MAP.get(entity.get("status"), entity.get("status"))
    row.grace_ends_at = None
    row.updated_at = datetime.now()
    db.commit()
    return {"rzp_status": row.rzp_status}


class VerifyRequest(BaseModel):
    razorpay_payment_id: str
    razorpay_subscription_id: str
    razorpay_signature: str


@router.post("/verify")
def verify_payment(
    body: VerifyRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    """HMAC-check-only reporting endpoint (design §5/§6): verifies the
    Checkout signature over `payment_id|subscription_id`, marks our payment
    row seen for UI responsiveness, returns the current plan snapshot. It
    NEVER grants entitlement — subscription.activated/.charged webhooks do
    all crediting. Clients can never mint credit here."""
    keys = _razorpay_keys()
    if keys is None:
        raise HTTPException(status_code=503, detail={"detail": "payments not configured"})
    _, key_secret = keys

    expected = hmac.new(
        key_secret.encode(),
        f"{body.razorpay_payment_id}|{body.razorpay_subscription_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, body.razorpay_signature):
        raise HTTPException(status_code=400, detail="Invalid payment signature")

    pay = (
        db.query(models.BillingPayment)
        .filter(
            models.BillingPayment.razorpay_payment_id == body.razorpay_payment_id,
            models.BillingPayment.user_id == user.id,
        )
        .first()
    )
    if pay is not None and pay.status == "created":
        pay.status = "seen"
        db.commit()

    snapshot = _sub_snapshot(db, user.id)
    snapshot.update({"verified": True})
    return snapshot


# --- webhook fan-out --------------------------------------------------------------


def _entity(event: dict, key: str) -> dict:
    return event.get("payload", {}).get(key, {}).get("entity", {}) or {}


def _resolve_user_id(db: Session, sub_entity: dict) -> int | None:
    notes = sub_entity.get("notes") or {}
    raw = notes.get("ffaa_user_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _apply_status(db: Session, sub: models.BillingSubscription, status: str) -> None:
    sub.rzp_status = status
    sub.updated_at = datetime.now()


def _handle_charged_like(
    db: Session,
    event_type: str,
    sub_entity: dict,
    payment_entity: dict,
    *,
    credit: bool,
) -> None:
    """subscription.charged / first charge on activated: amount-guarded,
    idempotent crediting through credit_payment (the ONLY period writer)."""
    rzp_sub_id = sub_entity.get("id") or ""
    sub = billing.get_sub_by_rzp_id(db, rzp_sub_id)
    user_id = sub.user_id if sub else _resolve_user_id(db, sub_entity)

    payment_id = payment_entity.get("id") or ""
    if not credit or not payment_id:
        return
    captured_paise = payment_entity.get("amount") or 0
    plan = billing.get_plan(db, sub.plan_code if sub else "pro")
    price_paise = (plan.price_rupees if plan else 0) * 100
    if price_paise > 0 and captured_paise < price_paise:
        # Amount guard (audit BILL-WEBHOOK-AMOUNT): a partial/manual capture
        # must never mint a full period.
        db.add(models.BillingPayment(
            user_id=user_id,
            razorpay_order_id=payment_entity.get("order_id"),
            razorpay_payment_id=payment_id,
            amount_rupees=captured_paise // 100,
            status="underpaid",
            created_at=datetime.now(),
        ))
        db.commit()
        return
    period_end = _parse_rzp_ts(sub_entity.get("current_end"))
    billing.credit_payment(
        db,
        user_id=user_id,
        payment_id=payment_id,
        order_id=payment_entity.get("order_id"),
        amount_rupees=captured_paise // 100,
        period_end=None if event_type == "subscription.activated" else period_end,
    )


WEBHOOK_HANDLERS = {}


def _handler(*event_types):
    def deco(fn):
        for et in event_types:
            WEBHOOK_HANDLERS[et] = fn
        return fn
    return deco


@_handler("subscription.authenticated")
def _on_authenticated(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "authenticated")


@_handler("subscription.activated")
def _on_activated(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub is None:
        uid = _resolve_user_id(db, sub_entity)
        if uid is None:
            return
        sub = models.BillingSubscription(
            user_id=uid, rzp_subscription_id=sub_entity.get("id"), plan_code="pro",
        )
        db.add(sub)
    _apply_status(db, sub, "active")
    sub.grace_ends_at = None  # recovery clears the grace clock
    _handle_charged_like(db, "subscription.activated", sub_entity, payment_entity, credit=True)


@_handler("subscription.charged")
def _on_charged(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "active")
        sub.grace_ends_at = None
    _handle_charged_like(db, "subscription.charged", sub_entity, payment_entity, credit=True)


@_handler("subscription.pending")
def _on_pending(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "past_due")
        # Grace clock starts at FIRST pending entry; later retry notices must
        # not push it out (halted keeps the same clock per design §1).
        if sub.grace_ends_at is None or sub.grace_ends_at <= datetime.now():
            sub.grace_ends_at = datetime.now() + timedelta(days=GRACE_DAYS)


@_handler("subscription.halted")
def _on_halted(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "past_due")  # grace clock unchanged


@_handler("subscription.paused")
def _on_paused(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "paused")


@_handler("subscription.resumed")
def _on_resumed(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "active")
        sub.grace_ends_at = None


@_handler("subscription.cancelled")
def _on_cancelled(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "cancelled")
        remaining = _parse_rzp_ts(sub_entity.get("current_end"))
        if remaining and remaining > datetime.now():
            sub.grace_ends_at = remaining  # Pro survives to period end
        else:
            sub.grace_ends_at = None


@_handler("subscription.completed")
def _on_completed(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "completed")


@_handler("subscription.expired")
def _on_expired(db, event, sub_entity, payment_entity):
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "expired")


@_handler("refund.processed")
def _on_refund(db, event, sub_entity, payment_entity):
    """Refund of a credited payment → mark refunded and clamp the period back
    to the refunded period's start if it hasn't elapsed (never below now)."""
    payment_id = payment_entity.get("id") or ""
    pay = (
        db.query(models.BillingPayment)
        .filter(models.BillingPayment.razorpay_payment_id == payment_id)
        .first()
    )
    if pay is None:
        return
    pay.status = "refunded"
    sub = billing.get_subscription(db, pay.user_id) if pay.user_id else None
    if (
        sub is not None
        and sub.current_period_end is not None
        and sub.current_period_end > datetime.now()
    ):
        start = sub.current_period_end - timedelta(days=billing.PERIOD_DAYS)
        sub.current_period_end = max(start, datetime.now())
    db.commit()


@_handler("payment.dispute.created")
def _on_dispute_created(db, event, sub_entity, payment_entity):
    """Open dispute → freeze the account: rzp_status='disputed' (a local
    extension), gate treats it as Free until resolution."""
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub:
        _apply_status(db, sub, "disputed")


@_handler("payment.dispute.won", "payment.dispute.closed")
def _on_dispute_resolved(db, event, sub_entity, payment_entity):
    """Resolution restores the mirrored RZP state; 'lost' stays frozen for
    manual ops (disputes are rare enough that no contest workflow exists)."""
    sub = billing.get_sub_by_rzp_id(db, sub_entity.get("id") or "")
    if sub and sub.rzp_status == "disputed":
        remote = sub_entity.get("status") or "active"
        _apply_status(db, sub, billing.RZP_STATUS_MAP.get(remote, remote))


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    # Verify HMAC over the RAW bytes BEFORE any parsing (rev #13).
    raw = await request.body()
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail={"detail": "payments not configured"})
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    actual = request.headers.get("x-razorpay-signature", "")
    if not hmac.compare_digest(expected, actual):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = json.loads(raw)
    event_id = event.get("id") or ""
    handler = WEBHOOK_HANDLERS.get(event.get("event"))

    # Replay guard: insert the event id FIRST; UNIQUE violation → duplicate
    # delivery → ack 200 no-op (makes ALL events replay-safe, not just charges).
    record = models.BillingWebhookEvent(
        event_id=event_id,
        event_type=event.get("event"),
        payload_json=raw.decode("utf-8", "replace"),
        received_at=datetime.now(),
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"status": "ok"}

    if handler is not None:
        handler(db, event, _entity(event, "subscription"), _entity(event, "payment"))
    record.processed_at = datetime.now()
    db.commit()
    # Verified webhooks always 200, even for untracked events/subscriptions.
    return {"status": "ok"}
