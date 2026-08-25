"""Cache RapidOCR raw texts for all kirana invoices (parser iteration source)."""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rapid_engine import extract_text_and_items  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "kirana_texts.json")
out = {}
for p in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "datasets", "kirana", "k_*.png"))):
    name = os.path.basename(p)
    t0 = time.time()
    text, items = extract_text_and_items(p)
    out[name] = {"raw_text": text, "nitems": len(items), "elapsed_s": round(time.time() - t0, 1)}
    print(f"{name}: {round(time.time()-t0,1)}s items={len(items)}", flush=True)
json.dump(out, open(OUT, "w"), indent=1)
print("saved", OUT)
