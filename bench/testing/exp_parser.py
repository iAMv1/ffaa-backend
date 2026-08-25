"""TESTING ONLY — experimental Indian-aware invoice parser. Not wired into app/.

A/B vs the production parser on real Indian invoices (RapidOCR raw text).

Indian grand-total rules under test:
1. Prefer 'Grand Total' / 'Amount Chargeable' over bare 'Total'.
2. Prefer a Total whose value line carries a currency token (₹/Rs/Rupees).
3. Exclude section totals (CGST/SGST/IGST/taxable blocks, line-item rows).
4. Validate with the amount-in-words block when present.

Usage: python bench/testing/exp_parser.py
"""
import importlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

TEXTS = json.load(open(os.path.join(os.path.dirname(__file__), "indian_texts.json")))
PROD = importlib.import_module("app.ocr")

CUR = r"[₹$]"


def _num(t):
    t = re.sub(r"[^0-9,.]", "", t or "").replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def exp_total(text):
    """Indian-aware grand total. Returns float or None."""
    lines = text.splitlines()
    # 1b) Indian invoices: the grand total sits just ABOVE "Amount Chargeable
    #     (in words)" / "(in words)" — last amount in the 3 lines before it.
    for i, ln in enumerate(lines):
        norm = re.sub(r"[^a-z]", "", ln.lower())
        if "inwords" in norm or "amountchargeable" in norm:
            for cand in reversed(lines[max(0, i - 3):i]):
                n = _num(cand)
                if n and n >= 100:
                    return n
    # 1) explicit Grand Total / Amount Chargeable with a value (same line or next)
    for i, ln in enumerate(lines):
        up = ln.upper()
        if "TAX" in up:
            continue
        if re.search(r"GRAND TOTAL|AMOUNT CHARGEABLE|TOTAL AMOUNT|TOTAL DUE", up):
            for cand in (ln, lines[i + 1] if i + 1 < len(lines) else ""):
                n = _num(cand)
                if n and n >= 100:
                    return n
    # 2) 'Total' rows whose following block carries a currency token (₹/Rs/Rupees)
    #    before the amount — grand-total style. Earliest match wins; amount is
    #    read AFTER the currency token (qty numbers precede ₹).
    for i, ln in enumerate(lines):
        if not re.search(r"\bTOTAL\b", ln.upper()):
            continue
        block = lines[i + 1:i + 5]
        cur_idx = next((j for j, b in enumerate(block)
                        if re.search(CUR + r"|Rs\.?|RUPEES", b.upper())), None)
        if cur_idx is None:
            continue
        for cand in block[cur_idx + 1:]:
            n = _num(cand)
            if n and n >= 100:
                return n
    # 3) amount-in-words validation target: last big Total-like value >= 100
    vals = []
    for ln in lines:
        if re.search(r"\bTOTAL\b", ln.upper()):
            n = _num(ln)
            if n and n >= 100:
                vals.append(n)
    return vals[-1] if vals else None


def prod_total(raw):
    f = PROD.parse_invoice_fields(raw)
    return f.get("total_amount")


GT = {"val_0.jpg": 16661.60, "train_0.jpg": 10667.00, "train_1.jpg": 14450.00,
      "train_2.jpg": 12720.00, "train_3.jpg": 14000.00, "train_4.jpg": 6780.00}

print(f"{'file':14s} {'prod':>10s} {'exp':>10s} {'gt':>10s}  prod_ok exp_ok")
p_ok = e_ok = n_gt = 0
for name, d in TEXTS.items():
    raw = d["raw_text"]
    pt, et = prod_total(raw), exp_total(raw)
    gt = GT.get(name)
    po = eo = None
    if gt:
        n_gt += 1
        po = pt is not None and abs(pt - gt) / gt <= 0.01
        eo = et is not None and abs(et - gt) / gt <= 0.01
        p_ok += int(po)
        e_ok += int(eo)
    print(f"{name:14s} {str(pt):>10} {str(et):>10} {str(gt):>10}  {po} {eo}")
print(f"\nGT-verified: prod {p_ok}/{n_gt}  exp {e_ok}/{n_gt}")
