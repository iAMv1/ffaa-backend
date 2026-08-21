"""TESTING ONLY — engine A/B: Paddle hybrid vs RapidOCR (stock / +our optimizations).

Passes (each doc once per OCR engine):
  A) paddle_hybrid : production app.ocr (v5 mobile + v6 medium fallback + our parser)
  B) rapid_stock   : RapidOCR raw text, parsed with OUR parser (engine comparison)
  C) rapid_prep    : RapidOCR on OUR preprocessed image (CLAHE+OTSU), OUR parser
  D) rapid_stock_raw: RapidOCR raw text, naive total regex (stock-level baseline)

Metrics per doc: time, grand_total vs GT, supplier vs GT (fuzzy), date vs GT.
Corpus: kirana (50, full GT) + kenil (22, invoice_no GT) + SROIE (5, box-derived GT).

Usage: python bench/testing/engine_ab.py [--paddle-only]
"""
import glob
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from rapidocr_onnxruntime import RapidOCR  # noqa: E402
from app.ocr import parse_invoice_fields, process_invoice_document  # noqa: E402

KIRANA = os.path.join(os.path.dirname(__file__), "datasets", "kirana")
KENIL = os.path.join(os.path.dirname(__file__), "indian_data")
SROIE = os.path.join(ROOT, "bench", "sroie2")

rapid = RapidOCR()


def rapid_text(path, preprocess=False):
    if preprocess:
        from app.ocr import preprocess_image
        path = preprocess_image(path)
    result, _ = rapid(path)
    return "\n".join(line[1] for line in result) if result else ""


def naive_total(text):
    m = re.findall(r"(?:Total|Grand Total)\s*[:\-]?\s*[₹$]?\s*([\d,]+\.\d{2})", text, re.I)
    return float(m[-1].replace(",", "")) if m else None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def total_ok(pred, gt):
    return pred is not None and gt and abs(pred - gt) / gt <= 0.01


def date_ok(pred, gt):
    if not pred or not gt:
        return False
    return norm(pred) in norm(gt) or norm(gt) in norm(pred)


docs = []
for p in sorted(glob.glob(os.path.join(KIRANA, "k_*.png"))):
    gt = json.load(open(p.replace(".png", ".gt.json")))
    docs.append(("kirana", os.path.basename(p), p, gt))
for p in sorted(glob.glob(os.path.join(KENIL, "*.jpg"))):
    g = os.path.join(KENIL, os.path.basename(p).replace(".jpg", ".gt.json"))
    gt = json.load(open(g)) if os.path.exists(g) else {}
    docs.append(("kenil", os.path.basename(p), p, gt))
for p in sorted(glob.glob(os.path.join(SROIE, "*.jpg"))):
    docs.append(("sroie", os.path.basename(p), p, {}))

RES = {"docs": len(docs)}


def score_one(kind, name, path, gt, engine, text):
    f = parse_invoice_fields(text)
    r = {"engine": engine, "time_s": 0}
    if kind == "kirana":
        r["total"] = total_ok(f.get("total_amount"), gt.get("grand_total"))
        sup = gt.get("supplier") or ""
        r["supplier"] = bool(sup and f.get("company_name") and
                             (norm(f.get("company_name"))[:8] in norm(sup) or norm(sup)[:8] in norm(f.get("company_name"))))
        r["date"] = date_ok(f.get("invoice_date"), gt.get("date"))
        r["items"] = len(gt.get("items") or []) and len(f.get("line_items") or []) == len(gt.get("items") or [])
    elif kind == "kenil":
        inv = (gt.get("gt_parse") or {}).get("invoice_no") if isinstance(gt, dict) else None
        r["invoice_no"] = bool(inv and norm(f.get("invoice_number")) == norm(inv))
    return r


results = []

for kind, name, path, gt in docs:
    t0 = time.time()
    pr = process_invoice_document(path)
    t_pad = round(time.time() - t0, 1)
    row = {"kind": kind, "file": name, "paddle_s": t_pad,
           "paddle_total": pr.get("total_amount"),
           "paddle_company": pr.get("company_name"),
           "paddle_date": pr.get("invoice_date")}
    row.update(score_one(kind, name, path, gt, "paddle", pr.get("raw_text", "")))
    t0 = time.time()
    raw = rapid_text(path)
    t_rs = round(time.time() - t0, 1)
    row["rapid_s"] = t_rs
    row["rapid_total"] = naive_total(raw)
    f = parse_invoice_fields(raw)
    row.update({f"rapid_{k}": v for k, v in score_one(kind, name, path, gt, "rapid_stock", raw).items() if k not in ("engine", "time_s")})
    row["rapid_total_parsed"] = f.get("total_amount")
    t0 = time.time()
    rawp = rapid_text(path, preprocess=True)
    row["rapid_prep_s"] = round(time.time() - t0, 1)
    fp = parse_invoice_fields(rawp)
    row.update({f"rapidp_{k}": v for k, v in score_one(kind, name, path, gt, "rapid_prep", rawp).items() if k not in ("engine", "time_s")})
    results.append(row)
    print(f"{kind:6s} {name:16s} paddle={t_pad:6.1f}s rapid={t_rs:6.1f}s prep={row['rapid_prep_s']:6.1f}s "
          f"pt={pr.get('total_amount')} rt={f.get('total_amount')} rpt={fp.get('total_amount')}", flush=True)

json.dump(results, open(os.path.join(os.path.dirname(__file__), "engine_ab.json"), "w"), indent=1, default=str)

# summary
for kind in ("kirana", "kenil", "sroie"):
    rs = [r for r in results if r["kind"] == kind]
    if not rs:
        continue
    print(f"\n== {kind} (n={len(rs)}) ==")
    for eng, pre in (("paddle", "paddle"), ("rapid_stock", "rapid_"), ("rapid_prep", "rapidp_")):
        tot = sum(1 for r in rs if r.get(pre + "total"))
        sup = sum(1 for r in rs if r.get(pre + "supplier")) if kind == "kirana" else None
        dat = sum(1 for r in rs if r.get(pre + "date")) if kind == "kirana" else None
        inv = sum(1 for r in rs if r.get(pre + "invoice_no")) if kind == "kenil" else None
        avg_t = sum(r.get(eng + "_s") or r.get("time_s") or 0 for r in rs) / len(rs)
        line = f"  {eng:12s} total={tot}/{len(rs)} avg={avg_t:.1f}s"
        if sup is not None:
            line += f" supplier={sup}/{len(rs)} date={dat}/{len(rs)}"
        if inv is not None:
            line += f" invoice_no={inv}/{len(rs)}"
        print(line)
