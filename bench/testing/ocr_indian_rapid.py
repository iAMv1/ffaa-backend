"""TESTING: OCR all Indian invoices with RapidOCR, dump raw text for parser A/B."""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from rapidocr_onnxruntime import RapidOCR  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "indian_texts.json")
rapid = RapidOCR()
out = {}
for p in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "indian_data", "*.jpg"))):
    name = os.path.basename(p)
    t0 = time.time()
    result, _ = rapid(p)
    text = "\n".join(line[1] for line in result) if result else ""
    out[name] = {"raw_text": text, "elapsed_s": round(time.time() - t0, 1)}
    print(f"{name}: {round(time.time()-t0,1)}s nlines={len(text.splitlines())}", flush=True)
json.dump(out, open(OUT, "w"), indent=1)
print("saved", OUT)
