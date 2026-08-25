import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.bank_parse import parse_bank_csv

for p in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "india_bank", "*"))):
    data = open(p, "rb").read()
    try:
        rows = parse_bank_csv(data)
        print(f"{os.path.basename(p):45s} rows={len(rows)}", rows[0] if rows else "-")
    except Exception as e:
        print(f"{os.path.basename(p):45s} ERROR {str(e)[:60]}")
