"""Pilot review tool: OCR every file in bench/pilot/ and dump fields to CSV.

Usage: python bench/pilot_review.py
Output: bench/pilot_review.csv — one row per file, extracted fields + source.
Purpose: fast eyeball verification of real Indian invoices (user-supplied).
"""
import csv
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ocr import process_invoice_document  # noqa: E402

PILOT = os.path.join(os.path.dirname(__file__), "pilot")
OUT = os.path.join(os.path.dirname(__file__), "pilot_review.csv")
FIELDS = ["invoice_number", "invoice_date", "company_name", "gst_rate",
          "taxable_value", "total_amount", "cgst", "sgst", "hsn_code",
          "quantity", "item_description"]

files = sorted(glob.glob(os.path.join(PILOT, "*")))
if not files:
    print("No files in bench/pilot/ — drop real invoices (jpg/png/pdf) there first.")
    sys.exit(1)

rows = []
for p in files:
    name = os.path.basename(p)
    t0 = time.time()
    try:
        r = process_invoice_document(p)
        row = {"file": name, "source": r.get("source", "?"),
               "items": len(r.get("line_items") or []),
               "elapsed_s": round(time.time() - t0, 1)}
        for f in FIELDS:
            row[f] = r.get(f) or ""
        rows.append(row)
        print(f"{name:40s} {row['source']:4s} {row['elapsed_s']:6.1f}s total={row['total_amount']} company={str(row['company_name'])[:24]}", flush=True)
    except Exception as e:
        rows.append({"file": name, "source": "ERROR", "elapsed_s": 0, "items": 0,
                     **{f: "" for f in FIELDS}})
        print(f"{name:40s} ERROR {str(e)[:60]}", flush=True)

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["file", "source", "elapsed_s", "items"] + FIELDS)
    w.writeheader()
    w.writerows(rows)
print(f"\nWrote {OUT} — {len(rows)} file(s). Verify fields, then tune parser on real data.")
