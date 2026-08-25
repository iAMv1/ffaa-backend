"""Spot-check: clean_print/two_col/merged with regex fallback; shadow with clahe+otsu."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ocr import process_invoice_document, parse_line_items  # noqa: E402
import cv2  # noqa: E402

GT = json.load(open(os.path.join(os.path.dirname(__file__), "gt.json")))
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def items_ok(pred_items, gt_items):
    if not gt_items:
        return 1.0
    if not pred_items:
        return 0.0
    from rapidfuzz import fuzz
    matched = 0
    used = set()
    for g in gt_items:
        for idx, p in enumerate(pred_items):
            if idx in used:
                continue
            if fuzz.ratio(p.get("desc") or "", g["desc"]) >= 70 and \
                    abs((p.get("taxable") or 0) - g["taxable"]) / max(g["taxable"], 1) <= 0.03:
                matched += 1
                used.add(idx)
                break
    return matched / len(gt_items)


for name in ("clean_print", "two_col", "merged_cells"):
    t0 = time.time()
    r = process_invoice_document(os.path.join(FIX, f"{name}.jpg"))
    sc = items_ok(r.get("line_items") or [], GT[name]["line_items"])
    print(f"{name:14s} items={sc:.2f} n={len(r.get('line_items') or [])} "
          f"({round(time.time()-t0,1)}s) desc={str(r.get('item_description'))[:30]!r}", flush=True)

# shadow: CLAHE(8,8) + OTSU instead of adaptive
t0 = time.time()
img = cv2.imread(os.path.join(FIX, "shadow_band.jpg"))
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
eq = clahe.apply(gray)
th = cv2.threshold(eq, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
tmp = os.path.join(os.environ.get("TEMP", "."), "shadow_variant.jpg")
cv2.imwrite(tmp, th)
from app.ocr import extract_text_and_items  # noqa: E402
text, items = extract_text_and_items(tmp)
import re
fields = {k: v for k, v in re.search(r"GST Rate: (\d+)", text).groups() if False} or {}
print(f"shadow clahe+otsu: total_match={'Total Amount: ' in text} "
      f"gst_rate={'18%' in text} hsn={'5208' in text} items={len(items)} "
      f"({round(time.time()-t0,1)}s)", flush=True)
