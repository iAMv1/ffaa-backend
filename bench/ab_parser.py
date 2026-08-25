"""Blind A/B: old parser (ocr_base) vs new (app.ocr) on cached real-doc texts."""
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cache = json.load(open(os.path.join(os.path.dirname(__file__), "texts_real.json")))
old = importlib.import_module("app.ocr_base")
new = importlib.import_module("app.ocr")

FIELD = ["invoice_number", "invoice_date", "company_name", "gst_rate",
         "taxable_value", "total_amount", "cgst", "sgst", "igst", "hsn_code"]
print(f"{'doc':34s} {'old hits':8s} {'new hits':8s}  old->new")
tot_o = tot_n = 0
for name, d in cache.items():
    try:
        fo = old.parse_invoice_fields(d["raw_text"])
    except Exception as e:
        fo = {"_crash": str(e)[:40]}
    try:
        fn = new.parse_invoice_fields(d["raw_text"])
    except Exception as e:
        fn = {"_crash": str(e)[:40]}
    if "_crash" in fo:
        print(f"{name:34s} OLD CRASHED: {fo['_crash']}")
        tot_o += 0
        tot_n += sum(1 for f in FIELD if fn.get(f))
        continue
    ho = sum(1 for f in FIELD if fo.get(f))
    hn = sum(1 for f in FIELD if fn.get(f))
    tot_o += ho
    tot_n += hn
    diffs = []
    for f in FIELD:
        a, b = fo.get(f), fn.get(f)
        if a != b:
            diffs.append(f"{f}: {str(a)[:22]!r}->{str(b)[:22]!r}")
    print(f"{name:34s} {ho:3d}      {hn:3d}     {'; '.join(diffs)[:80]}")
print(f"\nTOTAL fields old={tot_o} new={tot_n}")
