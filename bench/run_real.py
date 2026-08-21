"""Run current pipeline over bench/real/*, print per-doc timing + fields."""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ocr import process_invoice_document  # noqa: E402

docs = [p for p in glob.glob(os.path.join(os.path.dirname(__file__), "real", "*"))
        if not os.path.basename(p).startswith("_")]
t_all = time.time()
for p in docs:
    t0 = time.time()
    r = process_invoice_document(p)
    dt = round(time.time() - t0, 3)
    f = {k: v for k, v in r.items() if k not in ("raw_text",) and v}
    print(f"{os.path.basename(p):38s} {dt:7.3f}s  src={r.get('source')}  {json.dumps(f, default=str)[:220]}", flush=True)
print(f"TOTAL {len(docs)} docs: {round(time.time() - t_all, 2)}s", flush=True)
