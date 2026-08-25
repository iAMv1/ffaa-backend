"""Cache raw_text of real docs (fast, text path) for parser iteration."""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ocr import process_invoice_document  # noqa: E402

real = os.path.join(os.path.dirname(__file__), "real")
out = {}
for p in glob.glob(os.path.join(real, "*")):
    name = os.path.basename(p)
    if name.startswith("_") or name.endswith(".png"):
        continue  # scans: parser loop doesn't need OCR text
    t0 = time.time()
    r = process_invoice_document(p)
    out[name] = {"raw_text": r.get("raw_text", ""), "elapsed_s": round(time.time() - t0, 3)}
    print(name, round(time.time() - t0, 3), "s", flush=True)
json.dump(out, open(os.path.join(os.path.dirname(__file__), "texts_real.json"), "w"), indent=1)
print("saved texts_real.json")
