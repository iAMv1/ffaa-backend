"""Add GST/buyer columns to the invoices table. IDEMPOTENT — safe to re-run.

New columns (2026-08-26 OCR-quality pass):
  supplier_gstin  VARCHAR(15)
  buyer_name      VARCHAR(255)
  buyer_gstin     VARCHAR(15)
  place_of_supply VARCHAR(2)

SQLite's create_all never ALTERs existing tables, so new columns need an
explicit, repeatable migration. Honors --db like the other migrate_* scripts.
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "--db" in sys.argv:
    os.environ["DATABASE_URL"] = (
        f"sqlite:///{Path(sys.argv[sys.argv.index('--db') + 1]).as_posix()}"
    )

from sqlalchemy import inspect as sqla_inspect, text  # noqa: E402
from app.database import engine  # noqa: E402

COLUMNS = [
    ("supplier_gstin", "VARCHAR(15)"),
    ("buyer_name", "VARCHAR(255)"),
    ("buyer_gstin", "VARCHAR(15)"),
    ("place_of_supply", "VARCHAR(2)"),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="path to the sqlite db file")
    ap.parse_args()  # only consumed via the --db prelude above
    insp = sqla_inspect(engine)
    existing = {c["name"] for c in insp.get_columns("invoices")}
    added = []
    with engine.begin() as conn:
        for name, dtype in COLUMNS:
            if name in existing:
                continue
            conn.execute(text(f"ALTER TABLE invoices ADD COLUMN {name} {dtype}"))
            added.append(name)
    if added:
        print(f"[ok] added columns to invoices: {', '.join(added)}")
    else:
        print("[ok] invoices already has GST/buyer columns — nothing to do")


if __name__ == "__main__":
    main()
