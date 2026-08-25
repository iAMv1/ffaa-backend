"""Run engine once per fixture, dump raw_text cache for fast parser iteration.

Usage: python bench/cache_texts.py [variant]  -> bench/texts_<variant>.json
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

FIX = Path(__file__).parent / "fixtures"
variant = sys.argv[1] if len(sys.argv) > 1 else "app.ocr_base"
mod = __import__(variant, fromlist=["process_invoice_document"])

out = {}
for path in sorted(FIX.glob("*.jpg")):
    t0 = time.time()
    pred = mod.process_invoice_document(str(path))
    out[path.stem] = {
        "raw_text": pred.get("raw_text", ""),
        "fields": {k: v for k, v in pred.items() if k != "raw_text"},
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(path.stem, round(time.time() - t0, 1), "s")
json.dump(out, open(Path(__file__).parent / f"texts_{variant.split('.')[-1]}.json", "w"), indent=1)
print("saved texts_%s.json" % variant.split(".")[-1])
