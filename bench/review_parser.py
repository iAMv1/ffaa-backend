"""Parser review on cached real-doc text."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ocr import parse_invoice_fields  # noqa: E402

cache = json.load(open(os.path.join(os.path.dirname(__file__), "texts_real.json")))
for name, d in cache.items():
    f = parse_invoice_fields(d["raw_text"])
    got = {k: v for k, v in f.items() if k not in ("raw_text",) and v}
    print(
        f"{name:32s} date={got.get('invoice_date')} total={got.get('total_amount')} "
        f"company={str(got.get('company_name'))[:28]} items={len(got.get('line_items') or [])} "
        f"qty={got.get('quantity')}"
    )
