import io
import json
import os

import pandas as pd
from PIL import Image

OUT = os.path.join(os.path.dirname(__file__), "indian_inv")
os.makedirs(OUT, exist_ok=True)
df = pd.read_parquet(os.path.join(os.path.dirname(__file__), "indian_inv_val.parquet"))
n = 0
for i, r in df.iterrows():
    raw = r["image"]
    if isinstance(raw, dict) and "bytes" in raw:
        raw = raw["bytes"]
    im = Image.open(io.BytesIO(raw))
    p = os.path.join(OUT, f"val_{i}.jpg")
    im.convert("RGB").save(p, quality=92)
    json.dump(json.loads(r["ground_truth"]), open(os.path.join(OUT, f"val_{i}.gt.json"), "w"))
    n += 1
print("extracted", n, "validation invoices")
