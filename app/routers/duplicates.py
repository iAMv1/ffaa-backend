from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from .. import models, schemas
from ..database import SessionLocal
from ..services import scan_and_flag_duplicates
from ..tenancy import require_entitlement
from ..users import current_active_user

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/invoices/{invoice_id}/duplicates/check", response_model=schemas.DuplicateScanResult)
def check_duplicates(
    invoice_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    require_entitlement(db, user, "duplicate_scan")
    invoice = (
        db.query(models.Invoice)
        .join(models.Client, models.Invoice.client_id == models.Client.id)
        .filter(models.Invoice.id == invoice_id, models.Client.owner_id == user.id)
        .first()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    created = scan_and_flag_duplicates(db, invoice)
    db.commit()
    for flag in created:
        db.refresh(flag)

    return {
        "invoice_id": invoice.id,
        "flags_created": len(created),
        "matches": created,
    }


@router.get("/duplicates/flags", response_model=list[schemas.DuplicateFlagOut])
def list_flags(
    status: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    q = (
        db.query(models.DuplicateFlag)
        .join(models.Invoice, models.DuplicateFlag.invoice_id == models.Invoice.id)
        .join(models.Client, models.Invoice.client_id == models.Client.id)
        .filter(models.Client.owner_id == user.id)
    )
    if status:
        q = q.filter(models.DuplicateFlag.status == status)
    return q.order_by(models.DuplicateFlag.similarity_score.desc()).all()


@router.post("/duplicates/flags/{flag_id}/resolve", response_model=schemas.DuplicateFlagOut)
def resolve_flag(
    flag_id: int,
    resolve: schemas.DuplicateResolve,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    flag = (
        db.query(models.DuplicateFlag)
        .join(models.Invoice, models.DuplicateFlag.invoice_id == models.Invoice.id)
        .join(models.Client, models.Invoice.client_id == models.Client.id)
        .filter(models.DuplicateFlag.id == flag_id, models.Client.owner_id == user.id)
        .first()
    )
    if not flag:
        raise HTTPException(status_code=404, detail="Flag not found")
    if flag.status != "pending":
        raise HTTPException(status_code=400, detail="Flag already resolved")

    action = resolve.action.lower()
    if action not in ("accept", "reject"):
        raise HTTPException(status_code=400, detail="action must be accept or reject")

    flag.status = "accepted" if action == "accept" else "rejected"
    flag.reviewed_at = datetime.now()

    if action == "accept":
        flag.invoice.is_duplicate = True
        flag.invoice.duplicate_of = flag.potential_duplicate_id

    db.commit()
    db.refresh(flag)
    return flag
