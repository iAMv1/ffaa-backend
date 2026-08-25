"""Tenant-scoping + billing-entitlement helpers shared by tenant routers.

Tenancy model (plan): all tenant data hangs off Client → scoping is transitive
through client ownership. Object fetches MUST go through get_owned_client;
list queries filter `Client.owner_id == user.id`.
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import billing, models



def get_owned_client(db: Session, user: "models.User", client_id: int) -> models.Client:
    """Fetch a client owned by `user`; 404 on missing OR not-owned.

    404 (not 403) deliberately — never reveal other tenants' resource existence.
    """
    client = (
        db.query(models.Client)
        .filter(models.Client.id == client_id, models.Client.owner_id == user.id)
        .first()
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def require_entitlement(db: Session, user: "models.User", action: str) -> None:
    """Billing gate for every write/compute surface (plan rev #7).

    Resolves the caller's effective plan (active pro period → pro, else free)
    and enforces the plan's caps against current usage. Reads stay free.

    Cap-bearing actions:
      invoice_upload    → invoice_cap vs invoices created this calendar month
      client_create /
      client_auto_mint  → client_cap vs owned clients (rev #3: OCR mint too)

    Other gated actions (bank_upload, reconcile_run, duplicate_scan,
    reminder_send) carry no count cap on v1 plans; they pass through plan
    resolution so a future per-plan feature flag has one home here.

    Over-cap → 402 with an upgrade CTA payload.
    """
    code = billing.effective_plan_code(db, user.id)
    plan = billing.get_plan(db, code)
    if plan is None:
        # unseeded catalog — fail open rather than lock the app, but LOUDLY
        # (audit TENANCY-FAIL-OPEN): an empty billing_plans table silently
        # disables all caps, so make it visible in logs.
        import logging

        logging.getLogger("ffaa.tenancy").warning(
            "billing_plans empty — entitlement caps UNENFORCED; run seed_plans()"
        )
        return

    cap_field = {
        "invoice_upload": "invoice_cap",
        "client_create": "client_cap",
        "client_auto_mint": "client_cap",
    }.get(action)
    if cap_field is None:
        return
    cap = getattr(plan, cap_field)
    if cap is None:  # unlimited
        return

    if cap_field == "invoice_cap":
        month_start = datetime.now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        used = (
            db.query(func.count(models.Invoice.id))
            .join(models.Client, models.Invoice.client_id == models.Client.id)
            .filter(
                models.Client.owner_id == user.id,
                models.Invoice.created_at >= month_start,
            )
            .scalar()
        )
    else:
        used = (
            db.query(func.count(models.Client.id))
            .filter(models.Client.owner_id == user.id)
            .scalar()
        )

    if used >= cap:
        noun = "invoices per month" if cap_field == "invoice_cap" else "clients"
        raise HTTPException(
            status_code=402,
            detail={
                "detail": f"Your {plan.name} plan allows {cap} {noun} — upgrade to Pro.",
                "upgrade": True,
                "plan": code,
            },
        )
