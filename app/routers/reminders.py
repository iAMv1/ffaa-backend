from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta

from .. import models, schemas
from ..database import SessionLocal
from ..services import attempt_reminder, missing_docs, render_reminder

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/reminders/preview")
def reminder_preview(days: int = 30, db: Session = Depends(get_db)):
    since = datetime.now() - timedelta(days=days)
    # Grouped lookups instead of 4 queries per client (F-19b)
    inv_counts = dict(
        db.query(models.Invoice.client_id, func.count())
        .filter(models.Invoice.created_at >= since)
        .group_by(models.Invoice.client_id)
        .all()
    )
    bank_counts = dict(
        db.query(models.BankStatement.client_id, func.count())
        .filter(models.BankStatement.created_at >= since)
        .group_by(models.BankStatement.client_id)
        .all()
    )
    last_inv = dict(
        db.query(models.Invoice.client_id, func.max(models.Invoice.created_at))
        .group_by(models.Invoice.client_id)
        .all()
    )
    last_bank = dict(
        db.query(models.BankStatement.client_id, func.max(models.BankStatement.created_at))
        .group_by(models.BankStatement.client_id)
        .all()
    )

    out = []
    for c in db.query(models.Client).all():
        docs = []
        if not inv_counts.get(c.id):
            docs.append("Sales/Purchase invoices")
        if not bank_counts.get(c.id):
            docs.append("Bank statements")
        if not docs:
            continue
        last_candidates = [d for d in (last_inv.get(c.id), last_bank.get(c.id)) if d]
        last = max(last_candidates) if last_candidates else None
        subject, body = render_reminder(c.name, docs)
        out.append({
            "client_id": c.id,
            "name": c.name,
            "email": c.email,
            "missing_docs": docs,
            "last_upload": last.isoformat() if last else None,
            "days_since_upload": (datetime.now() - last).days if last else None,
            "subject": subject,
            "body": body,
        })
    return out


@router.post("/clients/{client_id}/send-reminder", response_model=schemas.EmailReminderOut)
def send_client_reminder(
    client_id: int,
    send: schemas.ReminderSend,
    db: Session = Depends(get_db),
):
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    days = send.days if send.days is not None else 30
    return attempt_reminder(
        db, client, days=days, send=send.send, custom_message=send.custom_message
    )


@router.get("/reminders/history", response_model=list[schemas.EmailReminderOut])
def reminder_history(client_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.EmailReminder).order_by(models.EmailReminder.created_at.desc())
    if client_id is not None:
        q = q.filter(models.EmailReminder.client_id == client_id)
    return q.all()


@router.delete("/reminders/{reminder_id}")
def delete_reminder(reminder_id: int, db: Session = Depends(get_db)):
    reminder = db.query(models.EmailReminder).filter(models.EmailReminder.id == reminder_id).first()
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")
    db.delete(reminder)
    db.commit()
    return {"deleted": reminder_id}


@router.get("/reminders/templates", response_model=list[schemas.ReminderTemplate])
def list_templates():
    from ..email_service import DEFAULT_TEMPLATE
    return [schemas.ReminderTemplate(**DEFAULT_TEMPLATE)]
