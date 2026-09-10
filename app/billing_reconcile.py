"""Nightly billing reconciliation (design §5, migration step 5).

    python -m app.billing_reconcile [--dry-run]

Run via cron / Windows Task Scheduler next to app.reminder_job. SQLite-safe,
single process. Heals lost webhooks and expires stale rows:

1. Remote drift heal — every local sub with rzp_status NOT IN (completed,
   cancelled, expired) older than 24h is fetched from Razorpay. Genuine drift
   in (rzp_status, current_period_end) is overwritten from RZP — EXCEPT local
   `lapsed`/`disputed` (LOCAL_STATUS_EXTENSIONS): those are never drift
   (billing.LOCAL_STATUS_EXTENSIONS docstring); only a genuine RZP transition
   webhook heals them. Missed charged payments are then credited via
   credit_payment (fetching the sub's paid invoices).
2. Stale rows — `created` older than 24h → expired; `past_due` beyond grace →
   `lapsed` (the ONLY place lapsed is set; grace_ends_at cleared).
3. Orphan sweep — payments `created` >48h with no matching RZP payment →
   `abandoned` (history hygiene). Needs keys; skipped keyless.
4. One summary line: counts checked/repaired — alertable, quiet otherwise.

Keyless posture: steps 2 run locally without network; remote steps skip.
"""
import argparse
from datetime import datetime, timedelta

from . import billing, models
from .database import SessionLocal

import logging

logger = logging.getLogger("ffaa.billing_reconcile")

try:
    from razorpay.errors import RazorpayError

    _RZP_ERRORS: tuple[type[Exception], ...] = (RazorpayError,)
except ImportError:  # SDK absent — remote steps cannot run anyway
    _RZP_ERRORS = (Exception,)


def _remote_period_end(entity: dict) -> datetime | None:
    raw = entity.get("current_end")
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw))
    except (TypeError, ValueError, OSError):
        return None


def _heal_remote_drift(db, client, dry_run: bool) -> dict:
    counts = {"checked": 0, "repaired": 0}
    if client is None:
        return counts
    horizon = datetime.now() - timedelta(hours=24)
    stale = []
    for s in db.query(models.BillingSubscription).all():
        if not s.rzp_subscription_id:
            continue
        if s.rzp_status in ("completed", "cancelled", "expired"):
            continue
        age_anchor = s.updated_at  # created rows carry their creation stamp
        if age_anchor is None or age_anchor > horizon:
            continue
        stale.append(s)

    for sub in stale:
        counts["checked"] += 1
        try:
            remote = client.subscription.fetch(sub.rzp_subscription_id)
        except _RZP_ERRORS:
            logger.warning(
                "RZP fetch failed for sub %s — leaving for the next night",
                sub.rzp_subscription_id, exc_info=True,
            )
            continue
        remote_status = billing.RZP_STATUS_MAP.get(
            remote.get("status"), remote.get("status")
        )
        remote_end = _remote_period_end(remote)
        drifted = (
            sub.rzp_status not in billing.LOCAL_STATUS_EXTENSIONS
            and (
                remote_status != sub.rzp_status
                or (remote_end is not None and remote_end != sub.current_period_end)
            )
        )
        if drifted:
            sub.rzp_status = remote_status
            sub.current_period_end = remote_end or sub.current_period_end
            counts["repaired"] += 1
        # Missed charged payments: credit any paid invoice we don't have.
        try:
            invoices = client.invoice.all({"subscription_id": sub.rzp_subscription_id})
        except _RZP_ERRORS:
            logger.warning(
                "RZP invoice fetch failed for sub %s — skipping missed-payment sweep",
                sub.rzp_subscription_id, exc_info=True,
            )
            invoices = {"items": []}
        for inv in invoices.get("items", []):
            if inv.get("status") != "paid":
                continue
            payment_id = inv.get("payment_id")
            if not payment_id:
                continue
            known = (
                db.query(models.BillingPayment)
                .filter(models.BillingPayment.razorpay_payment_id == payment_id)
                .first()
            )
            if known is not None and known.status == "paid":
                continue
            amount_paise = inv.get("amount_paid") or inv.get("amount") or 0
            plan = billing.get_plan(db, sub.plan_code)
            price_paise = (plan.price_rupees if plan else 0) * 100
            if price_paise > 0 and amount_paise < price_paise:
                continue  # same amount guard as webhooks
            if not dry_run:
                billing.credit_payment(
                    db,
                    user_id=sub.user_id,
                    payment_id=payment_id,
                    order_id=inv.get("order_id"),
                    amount_rupees=amount_paise // 100,
                    period_end=_remote_period_end(remote),
                )
                counts["repaired"] += 1
    if not dry_run:
        db.commit()
    return counts


def _expire_stale_rows(db, dry_run: bool) -> dict:
    now = datetime.now()
    horizon24 = now - timedelta(hours=24)
    counts = {"expired": 0, "lapsed": 0}
    for sub in db.query(models.BillingSubscription).all():
        if sub.rzp_status == "created" and sub.updated_at and sub.updated_at <= horizon24:
            sub.rzp_status = "expired"
            sub.updated_at = now
            counts["expired"] += 1
        elif (
            sub.rzp_status == "past_due"
            and sub.grace_ends_at is not None
            and sub.grace_ends_at <= now
        ):
            # The ONLY writer of `lapsed` anywhere in the codebase.
            sub.rzp_status = "lapsed"
            sub.grace_ends_at = None
            sub.updated_at = now
            counts["lapsed"] += 1
    if not dry_run:
        db.commit()
    return counts


def _sweep_orphans(db, client, dry_run: bool) -> int:
    if client is None:
        return 0
    horizon48 = datetime.now() - timedelta(hours=48)
    swept = 0
    orphaned = (
        db.query(models.BillingPayment)
        .filter(
            models.BillingPayment.status == "created",
            models.BillingPayment.created_at <= horizon48,
        )
        .all()
    )
    for pay in orphaned:
        try:
            client.payment.fetch(pay.razorpay_payment_id)
            continue  # exists on RZP — not an orphan
        except _RZP_ERRORS:
            logger.warning(
                "RZP payment fetch failed for %s — not marking abandoned",
                pay.razorpay_payment_id, exc_info=True,
            )
            continue
        if not dry_run:
            pay.status = "abandoned"
            db.commit()
        swept += 1
    return swept


def run_reconcile(db, client=None, dry_run: bool = False) -> dict:
    """Single reconciliation pass; returns the summary counters. `client` may
    be a test double (tests monkeypatch the SDK)."""
    drift = _heal_remote_drift(db, client, dry_run)
    stale = _expire_stale_rows(db, dry_run)
    abandoned = _sweep_orphans(db, client, dry_run)
    return {
        "checked": drift["checked"],
        "repaired": drift["repaired"],
        "expired": stale["expired"],
        "lapsed": stale["lapsed"],
        "abandoned": abandoned,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="FFAA nightly billing reconciliation")
    ap.add_argument("--dry-run", action="store_true", help="print only, no DB writes")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        summary = run_reconcile(db, client=billing._rzp_client(), dry_run=args.dry_run)
    finally:
        db.close()
    print(
        "billing_reconcile: checked={checked} repaired={repaired} "
        "expired={expired} lapsed={lapsed} abandoned={abandoned}".format(**summary)
    )


if __name__ == "__main__":
    main()
