"""P4 billing endpoints under /api/v1/billing.

/plans is public (landing pricing). Everything else requires a session.
Razorpay keys are read from env at request time — absent keys give the 503
"payments not configured" posture (plan: Billing model); tests monkeypatch env.
"""
import hashlib
import hmac
import json
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import billing, models
from ..database import SessionLocal
from ..users import current_active_user

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _razorpay_keys() -> tuple[str, str] | None:
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    return key_id, key_secret


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
        "status": sub.status if sub else "active",
        "current_period_end": (
            sub.current_period_end.isoformat() if sub and sub.current_period_end else None
        ),
        "payments": [
            {
                "id": p.id,
                "order_id": p.razorpay_order_id,
                "payment_id": p.razorpay_payment_id,
                "amount_rupees": p.amount_rupees,
                "status": p.status,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in payments
        ],
    }


class OrderRequest(BaseModel):
    plan_code: str = "pro"


@router.post("/order")
def create_order(
    body: OrderRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    plan = billing.get_plan(db, body.plan_code)
    if plan is None or plan.price_rupees <= 0:
        raise HTTPException(status_code=400, detail="Unknown or free plan")

    keys = _razorpay_keys()
    if keys is None:
        raise HTTPException(status_code=503, detail={"detail": "payments not configured"})
    key_id, key_secret = keys

    try:
        import razorpay  # lazy: SDK needed only when keys exist

        client = razorpay.Client(auth=(key_id, key_secret))
        order = client.order.create({
            "amount": plan.price_rupees * 100,  # paise
            "currency": "INR",
            "receipt": f"user:{user.id}:{datetime.now().strftime('%Y%m')}",
        })
    except Exception:
        # Bad/dummy keys or network failure → same graceful posture as keyless.
        raise HTTPException(
            status_code=503, detail={"detail": "payments not configured"}
        )

    pay = models.BillingPayment(
        user_id=user.id,
        razorpay_order_id=order["id"],
        amount_rupees=plan.price_rupees,
        status="created",
        created_at=datetime.now(),
    )
    db.add(pay)
    db.commit()
    return {
        "order_id": order["id"],
        "amount": plan.price_rupees,
        "key_id": key_id,
        "plan": plan.code,
    }


class VerifyRequest(BaseModel):
    order_id: str
    payment_id: str
    signature: str


@router.post("/verify")
def verify_payment(
    body: VerifyRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    keys = _razorpay_keys()
    if keys is None:
        raise HTTPException(status_code=503, detail={"detail": "payments not configured"})
    _, key_secret = keys

    expected = hmac.new(
        key_secret.encode(),
        f"{body.order_id}|{body.payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, body.signature):
        raise HTTPException(status_code=400, detail="Invalid payment signature")

    order_row = (
        db.query(models.BillingPayment)
        .filter(
            models.BillingPayment.razorpay_order_id == body.order_id,
            models.BillingPayment.user_id == user.id,
        )
        .first()
    )
    credited = billing.credit_payment(
        db,
        user_id=user.id,
        order_id=body.order_id,
        payment_id=body.payment_id,
        amount_rupees=order_row.amount_rupees if order_row else None,
    )
    sub = billing.get_subscription(db, user.id)
    return {
        "verified": True,
        "credited": credited,
        "plan_code": "pro",
        "current_period_end": (
            sub.current_period_end.isoformat() if sub and sub.current_period_end else None
        ),
    }


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    # Verify HMAC over the RAW bytes BEFORE any parsing (plan rev #13).
    raw = await request.body()
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail={"detail": "payments not configured"})
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    actual = request.headers.get("x-razorpay-signature", "")
    if not hmac.compare_digest(expected, actual):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = json.loads(raw)
    if event.get("event") == "payment.captured":
        entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        order_id = entity.get("order_id") or ""
        payment_id = entity.get("id") or ""
        order_row = (
            db.query(models.BillingPayment)
            .filter(models.BillingPayment.razorpay_order_id == order_id)
            .first()
        )
        if payment_id and order_row is not None:
            # Same idempotent writer as /verify — webhook + verify race-safe.
            billing.credit_payment(
                db,
                user_id=order_row.user_id,
                order_id=order_id,
                payment_id=payment_id,
                amount_rupees=(entity.get("amount") or 0) // 100,
            )
    # Verified webhooks always 200, even for untracked events/orders.
    return {"status": "ok"}
