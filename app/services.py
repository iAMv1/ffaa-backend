"""Shared domain services.

Single definition of business rules that more than one entry point needs:
duplicate scanning lives here (upload path + explicit re-scan endpoint),
and so does the reminder attempt flow (API route + scheduled job).
Services mutate the session but leave commit() to the caller unless noted,
so callers can batch writes.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models
from .duplicates import find_duplicates
from .email_service import send_email


# --- Duplicate detection -----------------------------------------------------

def scan_and_flag_duplicates(db: Session, invoice: models.Invoice) -> list[models.DuplicateFlag]:
    """Find near-duplicates of `invoice` among its client's other invoices and
    create pending DuplicateFlags for pairs not already flagged.

    Adds flags to the session WITHOUT committing; returns the new flags so
    callers can refresh/report after their own commit.
    """
    # Scope candidates (F-19c): same client, not already a duplicate, and a
    # plausible match — near-identical amount OR among the client's most
    # recent invoices. Full-table scans went quadratic with history size.
    amount = float(invoice.total_amount or 0.0)  # Decimal → float for the window math
    base = db.query(models.Invoice).filter(
        models.Invoice.client_id == invoice.client_id,
        models.Invoice.id != invoice.id,
        models.Invoice.is_duplicate == False,  # noqa: E712
    )
    if amount > 0:
        recent = (
            base.filter(
                models.Invoice.total_amount.between(amount * 0.9, amount * 1.1)
            )
            .order_by(models.Invoice.created_at.desc())
            .limit(200)
            .all()
        )
    else:
        recent = []
    fallback = (
        base.order_by(models.Invoice.created_at.desc())
        .limit(50)
        .all()
    )
    seen: set[int] = set()
    candidates: list[models.Invoice] = []
    for cand in recent + fallback:
        if cand.id not in seen:
            seen.add(cand.id)
            candidates.append(cand)
    created: list[models.DuplicateFlag] = []
    for other, score, fields in find_duplicates(invoice, candidates):
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
    return created


# --- Reminders ---------------------------------------------------------------

def missing_docs(db: Session, client_id: int, since: datetime) -> list[str]:
    """Document categories with zero uploads for this client since `since`."""
    docs = []
    inv_count = (
        db.query(models.Invoice)
        .filter(models.Invoice.client_id == client_id, models.Invoice.created_at >= since)
        .count()
    )
    if inv_count == 0:
        docs.append("Sales/Purchase invoices")
    bank_count = (
        db.query(models.BankStatement)
        .filter(models.BankStatement.client_id == client_id, models.BankStatement.created_at >= since)
        .count()
    )
    if bank_count == 0:
        docs.append("Bank statements")
    return docs


def render_reminder(client_name: str, docs: list[str]) -> tuple[str, str]:
    subject = f"Reminder: pending documents for {client_name}"
    body = (
        f"Dear {client_name},\n\n"
        "We are waiting for the following documents:\n"
        + "\n".join(f"- {d}" for d in docs)
        + "\n\nPlease share them at the earliest.\n\nThanks,\nAccounts Team\n"
    )
    return subject, body


def attempt_reminder(
    db: Session,
    client: models.Client,
    *,
    days: int = 30,
    send: bool = True,
    custom_message: str | None = None,
) -> models.EmailReminder:
    """Render + record one reminder for `client`, optionally sending it.

    Creates the EmailReminder row immediately (status pending), then either
    marks it failed/sent or leaves it pending when send=False. Commits each
    state change so history survives crashes.
    """
    since = datetime.now() - timedelta(days=days)
    docs = missing_docs(db, client.id, since)
    subject, body = render_reminder(client.name, docs)
    if custom_message and custom_message.strip():
        body = custom_message

    reminder = models.EmailReminder(
        client_id=client.id,
        subject=subject,
        body=body,
        status="pending",
        created_at=datetime.now(),
    )
    db.add(reminder)
    db.commit()
    db.refresh(reminder)

    if not send:
        return reminder

    if not client.email:
        reminder.status = "failed"
        reminder.error_message = "Client has no email"
        db.commit()
        db.refresh(reminder)
        return reminder

    try:
        send_email(client.email, subject, body)
        reminder.status = "sent"
        reminder.sent_at = datetime.now()
    except Exception as e:
        reminder.status = "failed"
        reminder.error_message = str(e)[:300]
    db.commit()
    db.refresh(reminder)
    return reminder
