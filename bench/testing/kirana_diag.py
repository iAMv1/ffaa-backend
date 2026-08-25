import json
import sys

sys.path.insert(0, ".")
from app.ocr import process_invoice_document

for i in (0, 1, 2):
    p = f"bench/testing/datasets/kirana/k_{i:03d}.png"
    gt = json.load(open(p.replace(".png", ".gt.json")))
    r = process_invoice_document(p)
    print(f"k_{i}: GT_total={gt.get('grand_total')} GT_sup={gt.get('supplier')[:20]!r}")
    print(f"   paddle: total={r.get('total_amount')} company={str(r.get('company_name'))[:24]!r} "
          f"date={r.get('invoice_date')} conf={r.get('confidence')} src={r.get('source')}")
