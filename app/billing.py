"""Billing core (Razorpay Subscriptions): plan catalog, entitlement helpers,
idempotent credit writer, webhook status mapping.

Kept free of FastAPI/router imports so tenancy.py and reminder_job.py can use
it directly. The razorpay SDK is only touched when keys exist (_rzp_client);
everything here works keyless/test-double.
"""
import json
import os
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from . import models
from .database import SessionLocal

PERIOD_DAYS = 30
GRACE_DAYS = 7


def _parse_rzp_ts(value) -> datetime | None:
    """RZP timestamps are unix seconds (single canonical parser)."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value))
    except (TypeError, ValueError, OSError):
        return None

# Local-only values stored in billing_subscriptions.rzp_status beside the
# mirrored Razorpay states. They are COMPUTED locally (nightly reconciliation
# sets `lapsed` when grace expires; an open dispute sets `disputed`). Per the
# design's reconciliation rule, a local extension paired with any live RZP
# state is NOT drift — the nightly job NEVER overwrites it (`lapsed` vs remote
# `halted`, `disputed` vs remote `active`); only a genuine RZP transition
# webhook clears them (activated/resumed/charged recovery, dispute resolution).
LOCAL_STATUS_EXTENSIONS = ("lapsed", "disputed")

# RZP subscription state -> local rzp_status column value.
RZP_STATUS_MAP = {
    "created": "created",
    "authenticated": "authenticated",
    "active": "active",
    "pending": "past_due",
    "halted": "past_due",
    "paused": "paused",
    "resumed": "active",
    "cancelled": "cancelled",
    "completed": "completed",
    "expired": "expired",
}

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


def catalog_ready() -> bool:
    """Boot invariant (design §4): False means billing_plans is empty — the
    app must refuse requests (503 billing_unconfigured) instead of silently
    disabling every cap (audit TENANCY-FAIL-OPEN)."""
    db = SessionLocal()
    try:
        return db.query(models.BillingPlan.code).first() is not None
    finally:
        db.close()


def _rzp_client():
    """SDK client when keys exist; None in test/keyless mode."""
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    import razorpay  # lazy: SDK needed only with keys
    return razorpay.Client(auth=(key_id, key_secret))


def ensure_rzp_plan(db, plan: models.BillingPlan) -> str | None:
    """Create-if-missing the paid plan on Razorpay Subscriptions API; cache
    rzp_plan_id on the catalog row (idempotent). Keyless → None (test mode
    crafts fixture ids)."""
    if plan.price_rupees <= 0:
        return None
    if plan.rzp_plan_id:
        return plan.rzp_plan_id
    client = _rzp_client()
    if client is None:
        return None
    rzp_plan = client.plan.create({
        "period": "monthly",
        "item": {
            "name": f"FFAA {plan.name}",
            "amount": plan.price_rupees * 100,  # paise
            "currency": "INR",
        },
    })
    plan.rzp_plan_id = rzp_plan["id"]
    db.commit()
    return plan.rzp_plan_id


def get_plan(db, code: str) -> models.BillingPlan | None:
    return db.get(models.BillingPlan, code)


def get_subscription(db, user_id: int) -> models.BillingSubscription | None:
    return (
        db.query(models.BillingSubscription)
        .filter(models.BillingSubscription.user_id == user_id)
        .first()
    )


def get_sub_by_rzp_id(db, rzp_subscription_id: str) -> models.BillingSubscription | None:
    return (
        db.query(models.BillingSubscription)
        .filter(models.BillingSubscription.rzp_subscription_id == rzp_subscription_id)
        .first()
    )


def effective_plan_code(db, user_id: int) -> str:
    """Grace-aware entitlement predicate (design §3), single code path:

        pro iff (rzp_status == 'active' OR now <= grace_ends_at)

    past_due/halted keep Pro through the 7-day grace window; cancelled keeps
    Pro until the remaining period end (grace set at cancel); lapsed/expired/
    completed carry no grace → Free instantly. An open dispute freezes the
    account to Free regardless of grace.
    """
    sub = get_subscription(db, user_id)
    if sub is None or sub.plan_code != "pro":
        return "free"
    if sub.rzp_status == "disputed":
        return "free"
    if sub.rzp_status == "active":
        return "pro"
    now = datetime.now()
    if sub.grace_ends_at is not None and now <= sub.grace_ends_at:
        return "pro"
    return "free"


def plan_is_active(db, user_id: int) -> bool:
    """Reminder-job gate: skip owners whose subscription has fully lapsed or
    terminally ended; plain free tenants (no subscription row) stay in."""
    sub = get_subscription(db, user_id)
    if sub is None:
        return True
    if sub.rzp_status == "lapsed":
        return False
    if (
        sub.rzp_status in ("expired", "completed")
        or (sub.rzp_status == "cancelled" and sub.current_period_end is not None)
    ) and (
        sub.grace_ends_at is None or sub.grace_ends_at <= datetime.now()
    ):
        return False
    return True


def extend_subscription(
    db, user_id: int, period_end: datetime | None = None
) -> datetime:
    """Grant/extend a pro period. With period_end (a renewal webhook's RZP
    `current_end`) the payload value wins verbatim but never shortens what we
    already granted; without it (first charge) we stack +PERIOD_DAYS from
    max(now, existing end) so early renewals never lose paid days."""
    now = datetime.now()
    sub = get_subscription(db, user_id)
    if sub is None:
        sub = models.BillingSubscription(user_id=user_id)
        db.add(sub)
    sub.plan_code = "pro"
    sub.rzp_status = "active"
    sub.grace_ends_at = None  # recovery clears the grace clock
    if period_end is not None:
        sub.current_period_end = max(period_end, sub.current_period_end or period_end)
    else:
        base = (
            sub.current_period_end
            if sub.current_period_end and sub.current_period_end > now
            else now
        )
        sub.current_period_end = base + timedelta(days=PERIOD_DAYS)
    sub.updated_at = now
    return sub.current_period_end


def credit_payment(
    db,
    *,
    user_id: int | None,
    payment_id: str,
    amount_rupees: int | None = None,
    order_id: str | None = None,
    period_end: datetime | None = None,
) -> bool:
    """Single idempotent credit writer (design §5) — the ONLY function that
    extends a period or flips a plan. Every crediting webhook funnels through
    it; /verify NEVER calls it.

    razorpay_payment_id is UNIQUE — replayed/racing deliveries credit exactly
    once; the second call is a no-op returning False. Renewals pass the RZP
    payload current_end as period_end; first charges pass None → now+30d.
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

    # Make sure a subscription row exists for this user even when the webhook
    # arrives before our own /subscriptions write (notes fallback path).
    if get_subscription(db, target_user) is None:
        db.add(models.BillingSubscription(user_id=target_user))
        db.flush()

    extend_subscription(db, target_user, period_end=period_end)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race to a concurrent credit for the same payment_id.
        db.rollback()
        return False
    return True
