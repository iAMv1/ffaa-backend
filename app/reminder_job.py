"""Overdue-client reminder job. Run via cron / Windows Task Scheduler.

    python -m app.reminder_job --days 30 [--dry-run]

Sends personalized reminder listing each client's missing document types
(invoices / bank statements) when nothing was uploaded in the last --days days.
Every attempt is logged to EmailReminder history.
"""
import argparse
from datetime import datetime, timedelta

from .database import SessionLocal
from . import models
from .email_service import send_email


def missing_docs(db, client_id: int, since: datetime) -> list[str]:
    docs = []
    inv = (
        db.query(models.Invoice)
        .filter(models.Invoice.client_id == client_id, models.Invoice.created_at >= since)
        .count()
    )
    if inv == 0:
        docs.append("Sales/Purchase invoices")
    bk = (
        db.query(models.BankStatement)
        .filter(models.BankStatement.client_id == client_id, models.BankStatement.created_at >= since)
        .count()
    )
    if bk == 0:
        docs.append("Bank statements")
    return docs


def render(client_name: str, docs: list[str]) -> tuple[str, str]:
    subject = f"Reminder: pending documents for {client_name}"
    body = (
        f"Dear {client_name},\n\n"
        "We are waiting for the following documents:\n"
        + "\n".join(f"- {d}" for d in docs)
        + "\n\nPlease share them at the earliest.\n\nThanks,\nAccounts Team\n"
    )
    return subject, body


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="overdue window in days")
    ap.add_argument("--dry-run", action="store_true", help="print only, no DB writes, no emails")
    args = ap.parse_args()

    since = datetime.now() - timedelta(days=args.days)
    db = SessionLocal()
    sent = failed = skipped = 0
    try:
        clients = db.query(models.Client).all()
        for c in clients:
            docs = missing_docs(db, c.id, since)
            if not docs:
                skipped += 1
                continue
            subject, body = render(c.name, docs)
            if args.dry_run:
                print(f"[dry] {c.name}: missing {docs}")
                skipped += 1
                continue

            reminder = models.EmailReminder(
                client_id=c.id, subject=subject, body=body,
                status="pending", created_at=datetime.now(),
            )
            db.add(reminder)
            db.commit()
            db.refresh(reminder)

            if not c.email:
                reminder.status = "failed"
                reminder.error_message = "Client has no email"
                db.commit()
                failed += 1
                print(f"[no-email] {c.name}")
                continue
            try:
                send_email(c.email, subject, body)
                reminder.status = "sent"
                reminder.sent_at = datetime.now()
                sent += 1
                print(f"[sent] {c.name} <{c.email}>")
            except Exception as e:
                reminder.status = "failed"
                reminder.error_message = str(e)
                failed += 1
                print(f"[failed] {c.name}: {e}")
            db.commit()
    finally:
        db.close()
    print(f"done: {sent} sent, {failed} failed, {skipped} skipped")


if __name__ == "__main__":
    main()
