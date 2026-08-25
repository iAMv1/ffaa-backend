"""Tenant-scoping helpers shared by every tenant-facing router.

Tenancy model (plan): all tenant data hangs off Client → scoping is transitive
through client ownership. Object fetches MUST go through get_owned_client;
list queries filter `Client.owner_id == user.id`.
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import models


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


def require_entitlement(user: "models.User", action: str) -> None:
    """Entitlement gate stub. P4 wires real billing caps here (402 + upgrade
    CTA payload when a free-plan write/compute exceeds its cap).

    Gate list (plan rev #7): invoice upload, bank upload, client create +
    auto-mint, reconcile run, duplicate scan, reminder send.
    """
    # TODO(P4): enforce billing_plans caps; P1 leaves entitlements unlimited.
    return None
