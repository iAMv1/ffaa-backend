"""Overdue-client reminder job. Run via cron / Windows Task Scheduler.

    python -m app.reminder_job --days 30 [--dry-run]

Sends personalized reminder listing each client's missing document types
(invoices / bank statements) when nothing was uploaded in the last --days days.
Every attempt is logged to EmailReminder history.

Business logic (missing_docs / render_reminder / attempt_reminder) lives in
app.services; this module is only the CLI loop.
"""
import argparse
from datetime import datetime, timedelta

from .database import SessionLocal
from . import models
from .services import attempt_reminder, missing_docs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="overdue window in days")
    ap.add_argument("--dry-run", action="store_true", help="print only, no DB writes, no emails")
    args = ap.parse_args()

    since = datetime.now() - timedelta(days=args.days)
    db = SessionLocal()
    sent = failed = skipped = 0
    try:
        # Skip tenants whose owner account is deactivated.
        # TODO(P4): gate on active plan — skip free-expired tenants here too
        # (plan rev #4). Global SMTP stays v1; per-user SMTP deferred.
        clients = (
            db.query(models.Client)
            .join(models.User, models.User.id == models.Client.owner_id)
            .filter(models.User.is_active == True)  # noqa: E712
            .all()
        )
        for c in clients:
            docs = missing_docs(db, c.id, since)
            if not docs:
                skipped += 1
                continue
            if args.dry_run:
                print(f"[dry] {c.name}: missing {docs}")
                skipped += 1
                continue

            reminder = attempt_reminder(db, c, days=args.days)
            if reminder.status == "sent":
                sent += 1
                print(f"[sent] {c.name} <{c.email}>")
            else:
                failed += 1
                print(f"[failed] {c.name}: {reminder.error_message}")
    finally:
        db.close()
    print(f"done: {sent} sent, {failed} failed, {skipped} skipped")


if __name__ == "__main__":
    main()
