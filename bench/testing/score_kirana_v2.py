"""Score exp_parser2 on kirana (text-only, fast iteration)."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exp_parser2 import exp_total, exp_supplier, exp_date  # noqa: E402
from app.ocr import parse_invoice_fields  # noqa: E402

TEXTS = json.load(open(os.path.join(os.path.dirname(__file__), "kirana_texts.json")))
KIRANA = os.path.join(os.path.dirname(__file__), "datasets", "kirana")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


t_old = t_new = s_old = s_new = d_new = 0
for name, d in sorted(TEXTS.items()):
    gt = json.load(open(os.path.join(KIRANA, name.replace(".png", ".gt.json"))))
    raw = d["raw_text"]
    old = parse_invoice_fields(raw)
    gt_total = gt.get("grand_total")
    ok_old = old.get("total_amount") is not None and abs(old.get("total_amount") - gt_total) / gt_total <= 0.01
    nt = exp_total(raw)
    ok_new = nt is not None and abs(nt - gt_total) / gt_total <= 0.01
    t_old += int(ok_old)
    t_new += int(ok_new)
    sup = gt.get("supplier") or ""
    s_old += int(bool(sup and old.get("company_name") and
                      (norm(old.get("company_name"))[:8] in norm(sup) or norm(sup)[:8] in norm(old.get("company_name")))))
    vs = exp_supplier(raw)
    s_new += int(bool(sup and vs and
                      (norm(vs)[:8] in norm(sup) or norm(sup)[:8] in norm(vs))))
    dd = exp_date(raw)
    d_new += int(bool(dd and gt.get("date") and norm(dd) in norm(gt.get("date"))))

print(f"totals    old={t_old}/50  new={t_new}/50")
print(f"suppliers old={s_old}/50  new={s_new}/50")
print(f"dates     new={d_new}/50")
