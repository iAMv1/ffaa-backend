"""Extract Indian invoice images + GT from parquet(s) into bench/testing/indian_data/."""
import io
import json
import os

import pandas as pd
from PIL import Image

OUT = os.path.join(os.path.dirname(__file__), "indian_data")
os.makedirs(OUT, exist_ok=True)


def extract(parquet, tag):
    df = pd.read_parquet(parquet)
    n = 0
    for i, r in df.iterrows():
        raw = r["image"]
        if isinstance(raw, dict) and "bytes" in raw:
            raw = raw["bytes"]
        im = Image.open(io.BytesIO(raw))
        p = os.path.join(OUT, f"{tag}_{i}.jpg")
        im.convert("RGB").save(p, quality=92)
        gt = r.get("ground_truth") or r.get("response") or "{}"
        if isinstance(gt, dict):
            gt = json.dumps(gt)
        json.dump(json.loads(gt), open(os.path.join(OUT, f"{tag}_{i}.gt.json"), "w"))
        n += 1
    print(f"{tag}: {n} images")


for f in ("indian_val.parquet", "indian_train.parquet"):
    p = os.path.join(os.path.dirname(__file__), f)
    if os.path.exists(p):
        extract(p, f.replace("indian_", "").replace(".parquet", ""))
