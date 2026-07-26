from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta

from .. import models, schemas
from ..database import SessionLocal
from ..email_service import send_reminder
from ..reminder_job import missing_docs, render

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
    out = []
    for c in db.query(models.Client).all():
        docs = missing_docs(db, c.id, since)
        last_inv = db.query(func.max(models.Invoice.created_at)).filter(
            models.Invoice.client_id == c.id
        ).scalar()
        last_bank = db.query(func.max(models.BankStatement.created_at)).filter(
            models.BankStatement.client_id == c.id
        ).scalar()
        last = max([d for d in (last_inv, last_bank) if d], default=None)
        if not docs:
            continue
        subject, body = render(c.name, docs)
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
    if not client.email:
        raise HTTPException(status_code=400, detail="Client has no email")

    reminder = models.EmailReminder(
        client_id=client_id,
        subject="",
        body="",
        status="pending",
        created_at=datetime.now(),
    )
    db.add(reminder)
    db.commit()
    db.refresh(reminder)

    try:
        subject, body = send_reminder(client.email, client.name, send.template_name)
        reminder.subject = subject
        reminder.body = body
        reminder.status = "sent"
        reminder.sent_at = datetime.now()
    except Exception as e:
        reminder.status = "failed"
        reminder.error_message = str(e)
        db.commit()
        db.refresh(reminder)
        raise HTTPException(status_code=500, detail=str(e))

    db.commit()
    db.refresh(reminder)
    return reminder


@router.get("/reminders/history", response_model=list[schemas.EmailReminderOut])
def reminder_history(client_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.EmailReminder).order_by(models.EmailReminder.created_at.desc())
    if client_id is not None:
        q = q.filter(models.EmailReminder.client_id == client_id)
    return q.all()


@router.get("/reminders/templates", response_model=list[schemas.ReminderTemplate])
def list_templates():
    from ..email_service import DEFAULT_TEMPLATE
    return [schemas.ReminderTemplate(**DEFAULT_TEMPLATE)]
