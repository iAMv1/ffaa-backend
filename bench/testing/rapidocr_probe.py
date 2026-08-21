"""TESTING ONLY — RapidOCR (ONNX INT8) probe. Does NOT touch app/ocr.py.

Benchmarks RapidOCR vs the current production hybrid on the same docs:
speed + extracted total/company. Production pipeline imported read-only for
comparison; RapidOCR is fully standalone.

Usage: python bench/testing/rapidocr_probe.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rapidocr_onnxruntime import RapidOCR  # noqa: E402

DOCS = [
    ("receipt", os.path.join("bench", "real", "receipt.png")),
    ("indian_val0", os.path.join("bench", "indian_inv", "val_0.jpg")),
    ("sroie_1", os.path.join("bench", "sroie2", "X51009447842.jpg")),
]

rapid = RapidOCR()


def rapid_text(path):
    result, _ = rapid(path)
    if not result:
        return ""
    return "\n".join(line[1] for line in result)


def our_pipeline(path):
    # production app/ocr.py — read-only comparison, no modification
    from app.ocr import process_invoice_document
    return process_invoice_document(path)


for name, p in DOCS:
    if not os.path.exists(p):
        print(f"{name}: missing {p}")
        continue
    # RapidOCR
    t0 = time.time()
    txt = rapid_text(p)
    rt = round(time.time() - t0, 1)
    # production
    t0 = time.time()
    r = our_pipeline(p)
    ot = round(time.time() - t0, 1)
    import re

    def find_total(t):
        m = re.search(r"(?:Total|Grand Total|TOTAL DUE)\s*[:\-]?\s*[₹$]?\s*([\d,]+\.\d{1,2})", t, re.I)
        return m.group(1) if m else None

    print(f"== {name} ==")
    print(f"  rapidocr: {rt:6.1f}s  total={find_total(txt)}  nlines={len(txt.splitlines())}")
    print(f"  prod    : {ot:6.1f}s  total={r.get('total_amount')}  conf={r.get('confidence')}")
