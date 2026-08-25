"""Seed demo data for testing multi-client workflows.

Usage:
    python seed.py          # seed only if clients table empty
    python seed.py --force  # wipe all rows, reseed
"""
import os
import sys
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, engine
from app import models

models.Base.metadata.create_all(bind=engine)

CLIENTS = [
    {"name": "Agarwal Textiles", "email": "accounts@agarwaltextiles.in", "gst_number": "27AAECA1234F1Z5"},
    {"name": "Bhumi Constructions", "email": None, "gst_number": "29AHFPB5678Q1Z2"},
    {"name": "Crescent Pharma Distributors", "email": "billing@crescentpharma.com", "gst_number": "36AALCC9012R1ZX"},
]

# (client_idx, number, days_ago, party, type, taxable, gst_rate, status, approved)
INVOICES = [
    (0, "AT/24-25/118", 42, "Shakti Garments", "sales", 48200, 5, "approved", True),
    (0, "AT/24-25/121", 30, "Mehta Saree House", "sales", 31750, 5, "approved", True),
    (0, "AT/24-25/122", 29, "Mehta Saree House", "sales", 31750, 5, "pending_review", False),  # near-dup of above
    (0, "AT/24-25/127", 18, "Kota Handloom Coop", "purchase", 66400, 5, "reviewed", False),
    (0, "AT/24-25/131", 9, "Shakti Garments", "sales", 27980, 5, "pending_review", False),
    (0, "AT/24-25/132", 4, "Vardhman Fabrics", "purchase", 91200, 12, "pending_review", False),
    (1, "BC/091", 35, "Ultratech Cement Ltd", "purchase", 214500, 28, "approved", True),
    (1, "BC/097", 21, "Shree Steel Traders", "purchase", 147300, 18, "reviewed", False),
    (1, "BC/102", 11, "Nagarjuna Infra", "sales", 386000, 18, "pending_review", False),
    (1, "BC/103", 6, "Local Hardware Mart", "purchase", 12850, 18, "pending_review", False),
    (2, "CPD/5521", 26, "Apollo Pharmacy Chain", "sales", 96400, 12, "approved", True),
    (2, "CPD/5534", 13, "Medlife Retail", "sales", 53725, 12, "reviewed", False),
    (2, "CPD/5539", 5, "Cipla Ltd", "purchase", 178900, 12, "pending_review", False),
    (2, "CPD/5540", 2, "Sun Pharma Distributors", "purchase", 246300, 12, "pending_review", False),
]

# (client_idx, days_ago, narration, debit, credit, reconciled)
BANK_ROWS = [
    (0, 40, "NEFT SHAKTI GARMENTS UTR4521", 0, 50610, True),
    (0, 28, "UPI MEHTA SAREE 9982XXXX", 0, 33337, False),
    (0, 17, "RTGS KOTA HANDLOOM COOP", 69720, 0, False),
    (0, 8, "CHQ 000214 CLEARING", 15000, 0, False),
    (0, 3, "NEFT SHAKTI GARMENTS UTR5907", 0, 29379, False),
    (1, 33, "RTGS ULTRATECH CEMENT", 225225, 0, True),
    (1, 19, "NEFT SHREE STEEL TRADERS", 173814, 0, False),
    (1, 10, "IMPS NAGARJUNA INFRA PART", 0, 200000, False),
    (1, 5, "CASH DEPOSIT BRANCH", 0, 45000, False),
    (2, 24, "NEFT APOLLO PHARMACY CHAIN", 0, 101220, True),
    (2, 12, "UPI MEDLIFE RETAIL", 0, 56411, False),
    (2, 4, "RTGS CIPLA LTD PAYABLE", 187845, 0, False),
]

REMINDERS = [
    (1, "Pending documents for March books", "sent", 7, None),
    (2, "GST data request — April", "failed", 2, "SMTP connection refused"),
]


def seed(db):
    now = datetime.now()
    # Operator resolved by email, not hardcoded id (audit SEED-HARDCODED-OWNER).
    operator = (
        db.query(models.User)
        .filter(models.User.email == os.environ.get("FFAA_OPERATOR_EMAIL", "operator@ffaa.local"))
        .first()
    )
    owner_id = operator.id if operator else 1
    clients = []
    for c in CLIENTS:
        client = models.Client(**c, owner_id=owner_id, created_at=now)
        db.add(client)
        clients.append(client)
    db.commit()

    invoices = []
    for ci, num, ago, party, itype, taxable, rate, status, approved in INVOICES:
        gst = round(taxable * rate / 100, 2)
        inv = models.Invoice(
            client_id=clients[ci].id,
            invoice_number=num,
            invoice_date=date.today() - timedelta(days=ago),
            company_name=party,
            gst_rate=rate,
            taxable_value=taxable,
            total_amount=round(taxable + gst, 2),
            cgst=round(gst / 2, 2),
            sgst=round(gst / 2, 2),
            igst=0.0,
            invoice_type=itype,
            status=status,
            approved=approved,
            ocr_confidence=round(0.62 + (hash(num) % 35) / 100, 2),
            created_at=now,
            reviewed_at=now if status in ("reviewed", "approved") else None,
        )
        db.add(inv)
        invoices.append(inv)
    db.commit()

    # near-duplicate pair: invoices idx 1 and 2 (Mehta Saree House)
    db.add(models.DuplicateFlag(
        invoice_id=invoices[2].id,
        potential_duplicate_id=invoices[1].id,
        similarity_score=91.4,
        matched_fields="invoice_number,company_name,total_amount",
        status="pending",
        created_at=now,
    ))
    # mark idx 2 as duplicate of idx 1
    invoices[2].is_duplicate = True
    invoices[2].duplicate_of = invoices[1].id
    db.add(models.InvoiceItem(
        invoice_id=invoices[1].id,
        description="Cotton fabric roll",
        hsn_code="5208",
        quantity=100,
        rate=482.0,
        taxable_value=48200.0,
        gst_rate=5.0,
        cgst=2410.0,
        sgst=2410.0,
        igst=0.0,
        line_total=53020.0,
    ))

    for ci, ago, narration, debit, credit, rec in BANK_ROWS:
        bal = round(credit - debit, 2)
        inv_id = None
        if ci == 0 and ago == 40:
            inv_id = invoices[0].id
        elif ci == 1 and ago == 33:
            inv_id = invoices[7].id
        elif ci == 2 and ago == 24:
            inv_id = invoices[12].id
        db.add(models.BankStatement(
            client_id=clients[ci].id,
            date=date.today() - timedelta(days=ago),
            narration=narration,
            debit=debit,
            credit=credit,
            balance=bal,
            reconciled=rec,
            invoice_id=inv_id if rec else None,
            created_at=now,
        ))

    for ci, subject, status, ago, err in REMINDERS:
        msg = "SMTP not configured (seed)" if status == "failed" and err and "SMTP" in err else err
        db.add(models.EmailReminder(
            client_id=clients[ci].id,
            subject=subject,
            body=f"Dear {clients[ci].name}, please share pending documents.",
            status=status,
            error_message=msg,
            sent_at=now - timedelta(days=ago) if status == "sent" else None,
            created_at=now - timedelta(days=ago),
        ))

    db.commit()
    print(f"Seeded: {len(CLIENTS)} clients, {len(INVOICES)} invoices, "
          f"{len(BANK_ROWS)} bank rows, 1 duplicate flag, {len(REMINDERS)} reminders")


def main():
    db = SessionLocal()
    try:
        if "--force" in sys.argv:
            for t in (models.DuplicateFlag, models.EmailReminder, models.Reconciliation,
                      models.BankStatement, models.InvoiceItem, models.Invoice, models.Client):
                db.query(t).delete()
            db.commit()
            print("Wiped existing rows")
        elif db.query(models.Client).count() > 0:
            print("Clients exist — skipping. Use --force to reseed.")
            return
        seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
