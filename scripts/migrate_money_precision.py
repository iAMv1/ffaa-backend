"""One-shot money precision normalizer (review F-08 / ticket 02).

SQLite doesn't enforce column types, so legacy rows may hold raw floats.
This script reads every money value through Decimal(str(v)) and rewrites it
rounded to 2dp, making sums exact under Numeric(14,2). Idempotent: values
already at 2dp rewrite to themselves. Run once after upgrading:

    python scripts/migrate_money_precision.py
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app import models  # noqa: E402

Q = Decimal("0.01")

MONEY_FIELDS = {
    models.Invoice: ("taxable_value", "total_amount", "cgst", "sgst", "igst"),
    models.InvoiceItem: ("rate", "taxable_value", "cgst", "sgst", "igst", "line_total"),
    models.BankStatement: ("debit", "credit", "balance"),
}


def main() -> None:
    db = SessionLocal()
    fixed = 0
    try:
        for model, fields in MONEY_FIELDS.items():
            for row in db.query(model).all():
                dirty = False
                for f in fields:
                    v = getattr(row, f)
                    if v is None:
                        continue
                    nv = Decimal(str(v)).quantize(Q)
                    if nv != v or not isinstance(v, Decimal):
                        setattr(row, f, nv)
                        dirty = True
                if dirty:
                    fixed += 1
        db.commit()
    finally:
        db.close()
    print(f"normalized {fixed} row(s)")


if __name__ == "__main__":
    main()
