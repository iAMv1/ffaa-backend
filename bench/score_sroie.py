"""Score pipeline on real SROIE scans. GT derived from task1 box text."""
import glob
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ocr import process_invoice_document  # noqa: E402
from app.bank_parse import _num  # noqa: E402

SROIE = os.path.join(os.path.dirname(__file__), "sroie2")


def gt_from_boxes(txt):
    lines = []
    for ln in txt.splitlines():
        m = re.match(r"^\d+(?:,\d+){7},(.*)$", ln)
        if m:
            lines.append(m.group(1))
    company = next((l for l in lines if len(l) >= 12 and l.isupper() and l.replace(" ", "").isalnum()), None)
    total = None
    for i, l in enumerate(lines):
        if "TOTAL" in l.upper():
            for cand in (l, lines[i + 1] if i + 1 < len(lines) else ""):
                nums = re.findall(r"[\d,]+\.\d{2}", cand)
                if nums:
                    total = _num(nums[-1])
                    break
            if total:
                break
    date = None
    for l in lines:
        m = re.search(r"(\d{2}/\d{2}/\d{4})", l)
        if m:
            date = m.group(1)
            break
    return {"company": company, "total": total, "date": date}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def fuzzy_ok(pred, gt):
    from rapidfuzz import fuzz
    return fuzz.ratio(norm(pred), norm(gt)) >= 70


hits = 0
for gt_path in sorted(glob.glob(os.path.join(SROIE, "*.txt"))):
    name = os.path.basename(gt_path).replace(".txt", "")
    gt = gt_from_boxes(open(gt_path, encoding="utf-8", errors="replace").read())
    t0 = time.time()
    try:
        r = process_invoice_document(os.path.join(SROIE, name + ".jpg"))
    except Exception as e:
        print(f"{name}: ERROR {str(e)[:50]}", flush=True)
        continue
    dt = round(time.time() - t0)
    o = {}
    if gt["company"]:
        o["company"] = fuzzy_ok(r.get("company_name") or "", gt["company"])
    if gt["total"]:
        tot = r.get("total_amount")
        o["total"] = tot is not None and abs(tot - gt["total"]) / max(gt["total"], 1) <= 0.01
    if gt["date"]:
        d = re.sub(r"[^0-9]", "", r.get("invoice_date") or "")
        o["date"] = bool(d) and (d in re.sub(r"[^0-9]", "", gt["date"])
                                 or re.sub(r"[^0-9]", "", gt["date"]) in d)
    h = sum(1 for v in o.values() if v)
    hits += h
    print(f"{name}: {h}/{len(o)} company={o.get('company')} total={o.get('total')} "
          f"date={o.get('date')} pred_total={r.get('total_amount')} gt_total={gt['total']} ({dt}s)", flush=True)
print(f"\nSROIE scans: {hits} hits total", flush=True)
