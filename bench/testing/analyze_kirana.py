"""Analyze kirana total/supplier failures vs GT. Run after cache_kirana.json."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.ocr import parse_invoice_fields  # noqa: E402

TEXTS = json.load(open(os.path.join(os.path.dirname(__file__), "kirana_texts.json")))
KIRANA = os.path.join(os.path.dirname(__file__), "datasets", "kirana")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


tot_ok = sup_ok = 0
fails = []
for name, d in sorted(TEXTS.items()):
    gt = json.load(open(os.path.join(KIRANA, name.replace(".png", ".gt.json"))))
    f = parse_invoice_fields(d["raw_text"])
    gt_total = gt.get("grand_total")
    ok = f.get("total_amount") is not None and abs(f.get("total_amount") - gt_total) / gt_total <= 0.01
    tot_ok += int(ok)
    sup = gt.get("supplier") or ""
    s_ok = bool(sup and f.get("company_name") and
                (norm(f.get("company_name"))[:8] in norm(sup) or norm(sup)[:8] in norm(f.get("company_name"))))
    sup_ok += int(s_ok)
    fails.append((name, ok, s_ok, f.get("total_amount"), gt_total, f.get("company_name"), sup))

print(f"totals {tot_ok}/50  suppliers {sup_ok}/50\n")
print("=== FAILURES (total wrong/missing) ===")
for name, ok, s_ok, pt, gt, comp, sup in fails:
    if ok:
        continue
    print(f"\n-- {name} pred={pt} GT={gt} company={str(comp)[:22]!r} gt_sup={str(sup)[:22]!r}")
    t = TEXTS[name]["raw_text"]
    lines = t.splitlines()
    for i, ln in enumerate(lines):
        if re.search(r"TOTAL|AMOUNT|GRAND|₹|Rs\.", ln.upper()):
            print(f"   {i}: {ln[:70]!r}")
