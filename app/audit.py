"""Deterministic invoice audit: GSTIN format + tax math consistency.

Pure Python, no AI. Catches OCR/extraction errors:
  - GSTIN must match the 15-char Indian format
  - taxable + cgst + sgst + igst should equal total
  - if a GST rate is set, taxable * rate% should equal the tax amount
"""
import re

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

# state code 01-38 (Indian states/UTs; 38 = Ladakh)
VALID_STATE_CODES = set(range(1, 39)) | {97}  # 97 = other territory


def validate_gstin(gstin: str | None) -> bool:
    if not gstin:
        return False
    g = gstin.strip().upper()
    if not GSTIN_RE.match(g):
        return False
    try:
        return int(g[:2]) in VALID_STATE_CODES
    except ValueError:
        return False


def math_check(taxable: float, cgst: float, sgst: float, igst: float,
               total: float, gst_rate: float | None) -> tuple[bool, str]:
    """Returns (ok, message)."""
    tax = (cgst or 0.0) + (sgst or 0.0) + (igst or 0.0)
    tol = max(1.0, abs(total) * 0.01)
    if abs((taxable or 0.0) + tax - total) > tol:
        return False, f"taxable+tax {round((taxable or 0)+tax,2)} != total {total}"
    if gst_rate and gst_rate > 0 and (taxable or 0.0) > 0:
        expected = (taxable or 0.0) * gst_rate / 100.0
        if abs(expected - tax) > max(1.0, expected * 0.05):
            return False, f"tax {round(tax,2)} != {gst_rate}% of taxable ({round(expected,2)})"
    return True, "ok"


def audit_invoice(invoice) -> dict:
    """Audit an Invoice ORM row. Returns {gstin_valid, math_ok, checks}."""
    gstin = getattr(invoice, "gst_number", None)
    if gstin is None:
        # invoices carry company GSTIN only implicitly — try client's GSTIN
        client = getattr(invoice, "client", None)
        gstin = getattr(client, "gst_number", None) if client else None
    gstin_valid = validate_gstin(gstin) if gstin else None
    math_ok, msg = math_check(
        invoice.taxable_value or 0.0, invoice.cgst or 0.0, invoice.sgst or 0.0,
        invoice.igst or 0.0, invoice.total_amount or 0.0, invoice.gst_rate,
    )
    return {"gstin_valid": gstin_valid, "math_ok": math_ok, "message": msg}
