import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session
from datetime import datetime
import os

from .. import models, schemas
from ..database import SessionLocal
from ..bank_parse import parse_bank_file
from ..reconcile import best_matches
from ..folders import archive_file

logger = logging.getLogger("ffaa.bank")

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/bank-statements/upload")
# Sync (not async): scanned-PDF parsing can OCR for minutes; `def` keeps the
# event loop free (review F-18).
def upload_bank(
    file: UploadFile = File(...),
    client_id: int = Query(...),
    preview: bool = Query(False),
    db: Session = Depends(get_db),
):
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    max_bytes = int(os.environ.get("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds MAX_FILE_SIZE_MB={os.environ.get('MAX_FILE_SIZE_MB', '50')}",
        )
    raw = file.file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds MAX_FILE_SIZE_MB={os.environ.get('MAX_FILE_SIZE_MB', '50')}",
        )

    # ponytail: keep temp copy for hierarchy archive
    upload_dir = "uploads/bank_statements"
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = (
        os.path.basename((file.filename or "bank").replace("\\", "/"))
        .replace("..", "").replace("/", "").replace("\\", "").strip()
        or "bank"
    )
    temp_path = os.path.join(
        upload_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
    )
    with open(temp_path, "wb") as f:
        f.write(raw)

    parsed = parse_bank_file(file.filename or "bank.csv", raw)
    if not parsed:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="No rows parsed (CSV/PDF)")

    if preview:
        # return parsed rows without inserting — FE shows before committing
        try:
            os.remove(temp_path)
        except OSError:
            pass
        rows = [schemas.BankStatementOut.model_validate({
            "id": 0, "client_id": client_id, "date": r["date"], "narration": r["narration"],
            "debit": r["debit"], "credit": r["credit"], "balance": r["balance"],
            "reconciled": False, "file_path": None,
        }) for r in parsed if r["date"]]
        return {"imported": len(rows), "rows": rows, "preview": True}

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
            balance=row.get("balance") or 0.0,
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


@router.delete("/bank-statements/{bank_id}")
def delete_bank_statement(bank_id: int, db: Session = Depends(get_db)):
    bs = db.query(models.BankStatement).filter(models.BankStatement.id == bank_id).first()
    if not bs:
        raise HTTPException(status_code=404, detail="Bank statement not found")
    db.query(models.Reconciliation).filter(
        models.Reconciliation.bank_statement_id == bank_id
    ).delete()
    db.delete(bs)
    db.commit()
    return {"deleted": bank_id}


@router.get("/reconciliations", response_model=list[schemas.ReconciliationOut])
def list_reconciliations(client_id: int | None = None, db: Session = Depends(get_db)):
    q = (
        db.query(
            models.Reconciliation,
            models.Invoice.invoice_number,
            models.BankStatement.narration,
            models.Invoice.total_amount,
        )
        .join(models.Invoice, models.Invoice.id == models.Reconciliation.invoice_id)
        .join(
            models.BankStatement,
            models.BankStatement.id == models.Reconciliation.bank_statement_id,
        )
    )
    if client_id is not None:
        q = q.filter(models.Invoice.client_id == client_id)
    out = []
    for rec, inv_no, narr, amt in q.order_by(models.Reconciliation.created_at.desc()).all():
        out.append(schemas.ReconciliationOut(
            id=rec.id, invoice_id=rec.invoice_id, invoice_number=inv_no,
            bank_statement_id=rec.bank_statement_id, narration=narr,
            amount=amt, match_score=rec.match_score, matched_by=rec.matched_by,
            confirmed=rec.confirmed, created_at=rec.created_at,
        ))
    return out


@router.delete("/reconciliations/{recon_id}")
def delete_reconciliation(recon_id: int, db: Session = Depends(get_db)):
    """Undo a match: remove the reconciliation row and reopen the bank row
    (bank row itself is kept)."""
    rec = db.query(models.Reconciliation).filter(models.Reconciliation.id == recon_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Reconciliation not found")
    bs = db.query(models.BankStatement).filter(
        models.BankStatement.id == rec.bank_statement_id).first()
    if bs:
        bs.reconciled = False
        bs.invoice_id = None
    db.delete(rec)
    db.commit()
    return {"deleted": recon_id}


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
