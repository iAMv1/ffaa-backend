"""Verify promoted production engine on all real corpora."""
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.ocr import process_invoice_document  # noqa: E402


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def run(files, kind, gt_of):
    tot = sup = dat = inv = 0
    times = []
    for p in files:
        name = os.path.basename(p)
        t0 = time.time()
        r = process_invoice_document(p)
        times.append(round(time.time() - t0, 1))
        gt = gt_of(p, name)
        if gt is None:
            print(f"{name}: {r.get('total_amount')} {str(r.get('company_name'))[:20]!r} ({times[-1]}s)", flush=True)
            continue
        gt_total = gt.get("grand_total")
        if gt_total:
            tot += int(r.get("total_amount") is not None and abs(r.get("total_amount") - gt_total) / gt_total <= 0.01)
        sup_n = gt.get("supplier") or ""
        if sup_n:
            sup += int(bool(r.get("company_name")) and
                       (norm(r.get("company_name"))[:8] in norm(sup_n) or norm(sup_n)[:8] in norm(r.get("company_name"))))
        d = gt.get("date")
        if d:
            dat += int(bool(r.get("invoice_date")) and norm(r.get("invoice_date")) in norm(d))
        print(f"{name}: total={r.get('total_amount')}/{gt_total} comp={str(r.get('company_name'))[:20]!r} ({times[-1]}s)", flush=True)
    n = len(files)
    print(f"\n== {kind} n={n} == total={tot}/{n} supplier={sup}/{n} date={dat}/{n} avg={sum(times)/max(n,1):.1f}s", flush=True)
    return tot, sup, dat, sum(times) / max(n, 1)


KIRANA = os.path.join(os.path.dirname(__file__), "datasets", "kirana")
KENIL = os.path.join(os.path.dirname(__file__), "indian_data")
SROIE = os.path.join(os.path.dirname(__file__), "..", "sroie2")

run(sorted(glob.glob(os.path.join(KIRANA, "k_*.png"))), "kirana",
    lambda p, n: json.load(open(os.path.join(KIRANA, n.replace(".png", ".gt.json")))))
run(sorted(glob.glob(os.path.join(KENIL, "*.jpg"))), "kenil",
    lambda p, n: None)
run(sorted(glob.glob(os.path.join(SROIE, "*.jpg"))), "sroie", lambda p, n: None)
