"""Extract kirana test invoices (image + GT json) into datasets/kirana/."""
import io
import json
import os

import pandas as pd
from PIL import Image

OUT = os.path.join(os.path.dirname(__file__), "datasets", "kirana")
os.makedirs(OUT, exist_ok=True)
df = pd.read_parquet(os.path.join(OUT, "test.parquet"))
n = 0
for i, r in df.iterrows():
    raw = r["image"]
    if isinstance(raw, dict) and "bytes" in raw:
        raw = raw["bytes"]
    im = Image.open(io.BytesIO(raw))
    p = os.path.join(OUT, f"k_{i:03d}.png")
    im.convert("RGB").save(p)
    resp = r["response"]
    if isinstance(resp, str):
        resp = json.loads(resp)
    json.dump(resp, open(os.path.join(OUT, f"k_{i:03d}.gt.json"), "w"), indent=1)
    n += 1
print("extracted", n, "kirana invoices")
