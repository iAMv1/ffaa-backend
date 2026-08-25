"""P4 billing core: plan catalog, entitlement helpers, idempotent credit writer.

Kept free of FastAPI/router imports so tenancy.py and reminder_job.py can use
it directly. Razorpay SDK is imported lazily inside the router (order creation
only) — everything here works keyless/test-double.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from . import models
from .database import SessionLocal

PERIOD_DAYS = 30

PLAN_SEEDS = [
    {
        "code": "free",
        "name": "Free",
        "price_rupees": 0,
        "invoice_cap": 10,   # invoices per calendar month
        "client_cap": 1,
        "features_json": json.dumps([
            "10 invoices / month", "1 client",
            "OCR extraction", "bank matching",
        ]),
    },
    {
        "code": "pro",
        "name": "Pro",
        "price_rupees": 499,
        "invoice_cap": None,
        "client_cap": None,
        "features_json": json.dumps([
            "Unlimited invoices & clients", "auto reconciliation",
            "duplicate detection", "email reminders", "priority support",
        ]),
    },
]


def seed_plans() -> None:
    """Idempotent plan seed — safe to call on every startup."""
    db = SessionLocal()
    try:
        for spec in PLAN_SEEDS:
            row = db.get(models.BillingPlan, spec["code"])
            if row is None:
                db.add(models.BillingPlan(**spec))
            else:
                for k, v in spec.items():
                    setattr(row, k, v)
        db.commit()
    finally:
        db.close()


def get_plan(db, code: str) -> models.BillingPlan | None:
    return db.get(models.BillingPlan, code)


def get_subscription(db, user_id: int) -> models.BillingSubscription | None:
    return (
        db.query(models.BillingSubscription)
        .filter(models.BillingSubscription.user_id == user_id)
        .first()
    )


def effective_plan_code(db, user_id: int) -> str:
    """'pro' iff an active subscription's period still covers now; else 'free'.

    Expired pro periods decay to the free tier automatically.
    """
    sub = get_subscription(db, user_id)
    if (
        sub is not None
        and sub.plan_code == "pro"
        and sub.status == "active"
        and sub.current_period_end is not None
        and sub.current_period_end > datetime.now()
    ):
        return "pro"
    return "free"


def plan_is_active(db, user_id: int) -> bool:
    """Reminder-job gate (plan rev #4): only owners whose billing period has
    *lapsed* are skipped; plain free tenants (no subscription row) stay in."""
    sub = get_subscription(db, user_id)
    if sub is not None and sub.current_period_end is not None \
            and sub.current_period_end <= datetime.now():
        return False
    return True


def extend_subscription(db, user_id: int) -> datetime:
    """Extend to max(now, existing end) + 30 days, mark active/pro.

    Stacking from the existing period end (not blind +30d) means a renewal
    before expiry never loses paid days (plan rev #6).
    """
    now = datetime.now()
    sub = get_subscription(db, user_id)
    base = (
        sub.current_period_end
        if sub and sub.current_period_end and sub.current_period_end > now
        else now
    )
    if sub is None:
        sub = models.BillingSubscription(user_id=user_id)
        db.add(sub)
    sub.plan_code = "pro"
    sub.status = "active"
    sub.current_period_end = base + timedelta(days=PERIOD_DAYS)
    sub.updated_at = now
    return sub.current_period_end


def credit_payment(
    db,
    *,
    user_id: int | None,
    order_id: str | None,
    payment_id: str,
    amount_rupees: int | None = None,
) -> bool:
    """Single idempotent credit writer (plan rev #6).

    razorpay_payment_id is UNIQUE — a replayed verify or a verify racing the
    webhook credits exactly once; the second call is a no-op returning False.
    """
    existing = (
        db.query(models.BillingPayment)
        .filter(models.BillingPayment.razorpay_payment_id == payment_id)
        .first()
    )
    if existing is not None:
        if existing.status == "paid":
            return False  # already credited — no double extension
        existing.status = "paid"
        target_user = existing.user_id
    else:
        pay = models.BillingPayment(
            user_id=user_id,
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            amount_rupees=amount_rupees or 0,
            status="paid",
            created_at=datetime.now(),
        )
        db.add(pay)
        target_user = user_id
    if target_user is None:
        db.commit()
        return True
    extend_subscription(db, target_user)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race to a concurrent credit for the same payment_id.
        db.rollback()
        return False
    return True
