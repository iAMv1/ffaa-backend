"""Score OCR pipeline variants against bench/gt.json.

Usage:
    python bench/score.py [variant]
variant: 'base' (default, current app.ocr) or module name like app.ocr_v2

Per-fixture per-field hit/miss + aggregate. Line items matched by fuzzy desc
(>=70) + amount rel err (<=3%). A/B output when called with a variant.
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

FIX = Path(__file__).parent / "fixtures"
GT = json.load(open(Path(__file__).parent / "gt.json"))

FIELD_NAMES = ["invoice_number", "invoice_date", "company_name", "gst_rate",
               "taxable_value", "total_amount", "cgst", "sgst", "igst",
               "hsn_code", "quantity", "item_description"]


def norm_str(s):
    if s is None:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def field_ok(field, pred, gt):
    if isinstance(gt, (int, float)) and gt == 0.0:
        # trivially satisfied (e.g. igst absent); None pred is a match
        return pred is None or pred == 0.0 or pred == ""
    if pred is None or pred == "" or pred == 0.0:
        return False
    if isinstance(gt, str):
        return norm_str(pred) == norm_str(gt)
    if isinstance(gt, (int, float)):
        return abs(float(pred) - float(gt)) / abs(float(gt)) <= 0.01
    return False


def fuzzy(a, b):
    from rapidfuzz import fuzz
    return fuzz.ratio(a, b)


def items_score(pred_items, gt_items):
    if not gt_items:
        return 1.0, 0, 0
    if not pred_items:
        return 0.0, len(gt_items), 0
    matched = 0
    used = set()
    for g in gt_items:
        best = None
        for idx, p in enumerate(pred_items):
            if idx in used:
                continue
            desc = (p.get("desc") or "")
            if desc and fuzzy(desc, g["desc"]) >= 70:
                amt_ok = abs((p.get("taxable") or 0) - g["taxable"]) / max(g["taxable"], 1) <= 0.03
                if best is None or (amt_ok and not best[1]):
                    best = (idx, amt_ok)
        if best and best[1]:
            matched += 1
            used.add(best[0])
    return matched / len(gt_items), len(gt_items) - matched, matched


def score_variant(modname):
    mod = __import__(modname, fromlist=["process_invoice_document"])
    proc = mod.process_invoice_document
    rows = []
    totals = {f: 0 for f in FIELD_NAMES}
    item_hits = item_total = 0
    conf_sum = 0.0
    for name, gt in GT.items():
        path = str(FIX / f"{name}.jpg")
        t0 = time.time()
        pred = proc(path)
        dt = round(time.time() - t0, 1)
        hits = {f: field_ok(f, pred.get(f), gt[f]) for f in FIELD_NAMES}
        for f in FIELD_NAMES:
            totals[f] += int(hits[f])
        conf = pred.get("confidence", 0.0) or 0.0
        conf_sum += conf
        row = {"fixture": name, "elapsed_s": dt, "conf": round(conf, 2),
               "hits": sum(hits.values()), "of": len(FIELD_NAMES), "fields": hits}
        row["items"] = items_score(pred.get("line_items") or [], gt.get("line_items") or [])
        item_hits += row["items"][2]
        item_total += len(gt.get("line_items") or [])
        rows.append(row)
        miss = [f for f in FIELD_NAMES if not hits[f]]
        print(f"{name:16s} {row['hits']:2d}/{row['of']} items {row['items'][0]:.2f} "
              f"conf {row['conf']:.2f} {dt:5.1f}s miss={','.join(miss) if miss else '-'}")
    f_total = sum(totals.values())
    f_all = len(FIELD_NAMES) * len(GT)
    overall = (f_total / f_all) * 0.7 + (item_hits / max(item_total, 1)) * 0.3
    print(f"\n== {modname} == fields {f_total}/{f_all} "
          f"({f_total / f_all:.3f}) items {item_hits}/{item_total} "
          f"({item_hits / max(item_total, 1):.3f}) overall {overall:.3f} "
          f"avg_conf {conf_sum / len(GT):.2f}")
    return {"overall": overall, "fields": f_total / f_all,
            "items": item_hits / max(item_total, 1), "conf": conf_sum / len(GT),
            "rows": rows}


if __name__ == "__main__":
    variant = sys.argv[1] if len(sys.argv) > 1 else "app.ocr"
    res = score_variant(variant)
    out = Path(__file__).parent / f"result_{variant.split('.')[-1]}.json"
    json.dump(res, open(out, "w"), indent=2)
    print("saved", out)
