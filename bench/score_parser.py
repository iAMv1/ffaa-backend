"""Parser-only scoring on cached OCR text. Fast loop for parser iteration.

Usage: python bench/score_parser.py [cache_name] [parser_variant]
  cache_name defaults to texts_base.json (engine cache)
  parser_variant defaults to app.ocr (parser under test)
"""
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

FIELD_NAMES = ["invoice_number", "invoice_date", "company_name", "gst_rate",
               "taxable_value", "total_amount", "cgst", "sgst", "igst",
               "hsn_code", "quantity", "item_description"]
GT = json.load(open(Path(__file__).parent / "gt.json"))


def norm_str(s):
    import re
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def field_ok(field, pred, gt):
    if pred is None or pred == "" or pred == 0.0:
        return False
    if isinstance(gt, str):
        return norm_str(pred) == norm_str(gt)
    if isinstance(gt, (int, float)):
        if gt == 0.0:
            return True
        return abs(float(pred) - float(gt)) / abs(float(gt)) <= 0.01
    return False


def items_score(pred_items, gt_items):
    from rapidfuzz import fuzz
    if not gt_items:
        return 1.0, 0
    if not pred_items:
        return 0.0, len(gt_items)
    matched = 0
    used = set()
    for g in gt_items:
        best = None
        for idx, p in enumerate(pred_items):
            if idx in used:
                continue
            desc = p.get("desc") or ""
            if desc and fuzz.ratio(desc, g["desc"]) >= 70:
                ok = abs((p.get("taxable") or 0) - g["taxable"]) / max(g["taxable"], 1) <= 0.03
                if best is None or ok:
                    best = (idx, ok)
        if best and best[1]:
            matched += 1
            used.add(best[0])
    return matched / len(gt_items), len(gt_items) - matched


def main():
    cache = sys.argv[1] if len(sys.argv) > 1 else "texts_base"
    parser_mod = sys.argv[2] if len(sys.argv) > 2 else "app.ocr"
    texts = json.load(open(Path(__file__).parent / f"{cache}.json"))
    parse = importlib.import_module(parser_mod).parse_invoice_fields

    totals = {f: 0 for f in FIELD_NAMES}
    item_matched = item_total = 0
    for name, gt in GT.items():
        pred = parse(texts[name]["raw_text"])
        hits = {f: field_ok(f, pred.get(f), gt[f]) for f in FIELD_NAMES}
        for f in FIELD_NAMES:
            totals[f] += int(hits[f])
        frac, missed = items_score(pred.get("line_items") or [], gt.get("line_items") or [])
        matched = len(gt.get("line_items") or []) - missed
        item_matched += matched
        item_total += len(gt.get("line_items") or [])
        miss = [f for f in FIELD_NAMES if not hits[f]]
        print(f"{name:16s} {sum(hits.values()):2d}/{len(FIELD_NAMES)} items {frac:.2f} miss={','.join(miss) if miss else '-'}")
    f_all = len(FIELD_NAMES) * len(GT)
    f_hit = sum(totals.values())
    print(f"\n== parser {parser_mod} on {cache} == fields {f_hit}/{f_all} ({f_hit/f_all:.3f}) items {item_matched}/{item_total} ({item_matched/max(item_total,1):.3f})")


if __name__ == "__main__":
    main()
