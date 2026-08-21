"""Prove PaddleOCR CPU on Windows. Exit 0 on success."""
import os
import sys

os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_use_mkldnn", "0")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FIX = os.path.join(ROOT, "tests", "fixtures", "sample_invoice.jpg")
if not os.path.isfile(FIX):
    print("missing fixture:", FIX, file=sys.stderr)
    sys.exit(2)

from app.ocr import extract_text, parse_invoice_fields  # noqa: E402

text = extract_text(FIX)
print(text[:2000] if text else "(empty)")
fields = parse_invoice_fields(text or "")
print("fields:", {k: v for k, v in fields.items() if k != "raw_text" and v})
sys.exit(0 if text.strip() else 1)
