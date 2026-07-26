from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from .. import models, schemas
from ..database import SessionLocal
from ..duplicates import find_duplicates

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/invoices/{invoice_id}/duplicates/check", response_model=schemas.DuplicateScanResult)
def check_duplicates(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # ponytail: scan same-client invoices only
    candidates = (
        db.query(models.Invoice)
        .filter(models.Invoice.client_id == invoice.client_id, models.Invoice.id != invoice.id)
        .all()
    )

    matches = find_duplicates(invoice, candidates)
    created = []
    for other, score, fields in matches:
        existing = (
            db.query(models.DuplicateFlag)
            .filter(
                (
                    (models.DuplicateFlag.invoice_id == invoice.id)
                    & (models.DuplicateFlag.potential_duplicate_id == other.id)
                )
                | (
                    (models.DuplicateFlag.invoice_id == other.id)
                    & (models.DuplicateFlag.potential_duplicate_id == invoice.id)
                )
            )
            .first()
        )
        if existing:
            continue
        flag = models.DuplicateFlag(
            invoice_id=invoice.id,
            potential_duplicate_id=other.id,
            similarity_score=score,
            matched_fields=fields,
            status="pending",
            created_at=datetime.now(),
        )
        db.add(flag)
        created.append(flag)

    db.commit()
    for flag in created:
        db.refresh(flag)

    return {
        "invoice_id": invoice.id,
        "flags_created": len(created),
        "matches": created,
    }


@router.get("/duplicates/flags", response_model=list[schemas.DuplicateFlagOut])
def list_flags(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(models.DuplicateFlag)
    if status:
        q = q.filter(models.DuplicateFlag.status == status)
    return q.order_by(models.DuplicateFlag.similarity_score.desc()).all()


@router.post("/duplicates/flags/{flag_id}/resolve", response_model=schemas.DuplicateFlagOut)
def resolve_flag(flag_id: int, resolve: schemas.DuplicateResolve, db: Session = Depends(get_db)):
    flag = db.query(models.DuplicateFlag).filter(models.DuplicateFlag.id == flag_id).first()
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
