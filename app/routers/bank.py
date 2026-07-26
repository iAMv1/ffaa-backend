from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session
from datetime import datetime
import os
import shutil

from .. import models, schemas
from ..database import SessionLocal
from ..bank_parse import parse_bank_file
from ..reconcile import best_matches
from ..folders import archive_file

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/bank-statements/upload")
async def upload_bank(
    file: UploadFile = File(...),
    client_id: int = Query(...),
    db: Session = Depends(get_db),
):
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    raw = await file.read()

    # ponytail: keep temp copy for hierarchy archive
    upload_dir = "uploads/bank_statements"
    os.makedirs(upload_dir, exist_ok=True)
    temp_path = os.path.join(
        upload_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename or 'bank'}"
    )
    with open(temp_path, "wb") as f:
        f.write(raw)

    parsed = parse_bank_file(file.filename or "bank.csv", raw)
    if not parsed:
        raise HTTPException(status_code=400, detail="No rows parsed (CSV/PDF)")

    # ponytail: archive entire statement file once
    stmt_date = parsed[0]["date"] if parsed[0].get("date") else datetime.now().date()
    archived = archive_file(
        temp_path, client.name, stmt_date, "Bank Statements", os.path.basename(temp_path)
    )

    created = []
    now = datetime.now()
    for row in parsed:
        if not row["date"]:
            continue
        bs = models.BankStatement(
            client_id=client_id,
            date=row["date"],
            narration=row["narration"],
            debit=row["debit"],
            credit=row["credit"],
            balance=row["balance"],
            file_path=archived,
            created_at=now,
        )
        db.add(bs)
        created.append(bs)
    db.commit()
    for bs in created:
        db.refresh(bs)
    return {
        "imported": len(created),
        "rows": [schemas.BankStatementOut.model_validate(b) for b in created],
    }


@router.get("/bank-statements", response_model=list[schemas.BankStatementOut])
def list_bank(client_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.BankStatement)
    if client_id is not None:
        q = q.filter(models.BankStatement.client_id == client_id)
    return q.order_by(models.BankStatement.date.desc()).all()


@router.post("/reconcile")
def run_reconcile(
    client_id: int = Query(...),
    min_score: float = Query(0.55),
    confirm: bool = Query(False),
    db: Session = Depends(get_db),
):
    invoices = (
        db.query(models.Invoice)
        .filter(
            models.Invoice.client_id == client_id,
            models.Invoice.approved == True,  # noqa: E712
            models.Invoice.is_duplicate == False,  # noqa: E712
        )
        .all()
    )
    banks = (
        db.query(models.BankStatement)
        .filter(
            models.BankStatement.client_id == client_id,
            models.BankStatement.reconciled == False,  # noqa: E712
        )
        .all()
    )
    matches = best_matches(invoices, banks, min_score=min_score)
    results = []
    now = datetime.now()
    for inv, br, score in matches:
        item = {
            "invoice_id": inv.id,
            "invoice_number": inv.invoice_number,
            "bank_statement_id": br.id,
            "narration": br.narration,
            "amount": inv.total_amount,
            "match_score": round(score, 3),
        }
        if confirm:
            db.add(
                models.Reconciliation(
                    invoice_id=inv.id,
                    bank_statement_id=br.id,
                    match_score=score,
                    matched_by="auto",
                    confirmed=True,
                    created_at=now,
                )
            )
            br.reconciled = True
            br.invoice_id = inv.id
        results.append(item)
    if confirm:
        db.commit()
    return {"matches": results, "confirmed": confirm}
