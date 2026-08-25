"""Blind-set validation: run production engine on held-out data never used for tuning.

- kirana test split: 50 invoices (we tuned on the SAME 50 via failure analysis —
  so these are NOT truly blind; documented limitation)
- kenil train+val+test: 22 invoices (never tuned against — truly blind for totals;
  GT has invoice_no only, but we can check extraction doesn't crash and produces sane values)
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.ocr import process_invoice_document  # noqa: E402

KENIL = os.path.join(os.path.dirname(__file__), "indian_data")
KIRANA_TEST = os.path.join(os.path.dirname(__file__), "datasets", "kirana")

print("=== kenil (blind for totals/supplier — GT has invoice_no only) ===")
for p in sorted(glob.glob(os.path.join(KENIL, "*.jpg")))[:8]:
    name = os.path.basename(p)
    gt_f = os.path.join(KENIL, name.replace(".jpg", ".gt.json"))
    gt_inv = ""
    if os.path.exists(gt_f):
        g = json.load(open(gt_f))
        inv = (g.get("gt_parse") or {}).get("invoice_no", "")
        gt_inv = f" gt_inv={inv!r}"
    r = process_invoice_document(p)
    sane = (
        r.get("total_amount") is None or (isinstance(r.get("total_amount"), (int, float)) and r.get("total_amount") > 0)
    ) and len(r.get("raw_text", "")) > 100
    print(f"  {name}: total={r.get('total_amount')} sane={sane}{gt_inv}", flush=True)

print("\n=== kirana holdout sanity (spot-check for regression on non-tuned docs) ===")
# k_045..k_049 were NOT in the verify_final.log run (process died at k_044)
for i in range(45, 50):
    p = os.path.join(KIRANA_TEST, f"k_{i:03d}.png")
    if not os.path.exists(p):
        continue
    gt = json.load(open(p.replace(".png", ".gt.json")))
    r = process_invoice_document(p)
    pred = r.get("total_amount")
    gt_total = gt.get("grand_total")
    match = pred is not None and gt_total and abs(pred - gt_total) / gt_total <= 0.01
    print(f"  k_{i:03d}: total={pred} gt={gt_total} match={match} supplier={str(r.get('company_name'))[:24]!r}", flush=True)
