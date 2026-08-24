"""Golden characterization tests for ocr.parse_invoice_fields total rules.

Pins CURRENT parser behavior: GRAND TOTAL > "in words" marker >
line-start TOTAL (last match wins) > SUBTOTAL fallback.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.ocr import parse_invoice_fields


def test_grand_total_rule_wins():
    text = (
        "TAX INVOICE\n"
        "Invoice No.: INV-GT-01\n"
        "Dated: 25/12/2024\n"
        "From: Acme Traders\n"
        "Widget 2 500.00 1,000.00\n"
        "GRAND TOTAL : 1180.00\n"
    )
    f = parse_invoice_fields(text)
    # GRAND TOTAL beats any other total-looking line (there is none here,
    # but the rule must fire on the merged-word-tolerant label too).
    assert f["total_amount"] == 1180.0
    assert f["invoice_number"] == "INV-GT-01"
    assert f["invoice_date"] == "25/12/2024"  # string passthrough, no parsing
    assert f["company_name"] == "Acme Traders"


def test_grand_total_value_on_next_line():
    text = (
        "TAX INVOICE\n"
        "GRAND TOTAL\n"
        "Rs. 2,500.00\n"
    )
    f = parse_invoice_fields(text)
    assert f["total_amount"] == 2500.0


def test_in_words_marker_takes_total_above_it():
    text = (
        "TAX INVOICE\n"
        "SUBTOTAL: 5,000.00\n"
        "CGST: 450.00\n"
        "SGST: 450.00\n"
        "TOTAL Rs.5,900.00\n"
        "Amount Chargeable (in words): INR Five Thousand Nine Hundred Only\n"
        "Invoice No.: INV-IW-9\n"
    )
    f = parse_invoice_fields(text)
    # "in words" marker rule: nearest numeric >= 100 above the marker wins
    assert f["total_amount"] == 5900.0
    assert f["invoice_number"] == "INV-IW-9"


def test_line_start_total_last_match_wins():
    text = (
        "TAX INVOICE\n"
        "TOTAL: 1,000.00\n"
        "SUBTOTAL: 900.00\n"
        "Taxable Value: 1000.00\n"
        "GST Rate: 18%\n"
        "TOTAL AMOUNT: 1180.00\n"
    )
    f = parse_invoice_fields(text)
    # multi-section bills: LAST line-start TOTAL wins
    assert f["total_amount"] == 1180.0
    assert f["taxable_value"] == 1000.0
    assert f["gst_rate"] == 18.0


def test_subtotal_fallback_when_only_subtotal():
    text = (
        "TAX INVOICE\n"
        "SUBTOTAL : 850.00\n"
    )
    f = parse_invoice_fields(text)
    assert f["total_amount"] == 850.0


def test_subtotal_before_gst_is_not_used_as_total():
    # "SUBTOTAL (BEFORE GST)" is explicitly excluded from the fallback
    text = (
        "TAX INVOICE\n"
        "SUBTOTAL (BEFORE GST): 850.00\n"
    )
    f = parse_invoice_fields(text)
    assert f["total_amount"] is None


def test_totals_below_100_are_ignored():
    # _grab rejects candidate totals < 100 (noise guard)
    text = (
        "TAX INVOICE\n"
        "TOTAL: 42.00\n"
    )
    f = parse_invoice_fields(text)
    assert f["total_amount"] is None


def test_gst_rate_and_explicit_cgst_sgst_split():
    text = (
        "TAX INVOICE\n"
        "Taxable Value: 5000.00\n"
        "GST Rate: 18%\n"
        "CGST: 450.00\n"
        "SGST: 450.00\n"
        "TOTAL AMOUNT: 5900.00\n"
    )
    f = parse_invoice_fields(text)
    assert f["gst_rate"] == 18.0
    assert f["taxable_value"] == 5000.0
    assert f["cgst"] == 450.0
    assert f["sgst"] == 450.0
    assert f["igst"] is None


def test_bare_gst_amount_splits_half_cgst_half_sgst():
    # OCR often drops the leading C/S: bare "GST: X" splits across both
    text = (
        "TAX INVOICE\n"
        "GST: 4,500.00\n"
        "TOTAL AMOUNT: 29500.00\n"
    )
    f = parse_invoice_fields(text)
    assert f["cgst"] == 2250.0
    assert f["sgst"] == 2250.0


def test_igst_captured():
    text = (
        "TAX INVOICE\n"
        "IGST: 900.00\n"
        "TOTAL AMOUNT: 5900.00\n"
    )
    f = parse_invoice_fields(text)
    assert f["igst"] == 900.0
    assert f["cgst"] is None
    assert f["sgst"] is None


def test_invoice_number_stops_at_dated_label():
    # char-class absorbs following words; parser must cut at date labels
    text = "Invoice No.: INV-77 Dated 15/03/2025\n"
    f = parse_invoice_fields(text)
    assert f["invoice_number"] == "INV-77"


def test_month_name_date_fallback_normalized():
    text = (
        "RECEIPT\n"
        "January 5, 2019\n"
    )
    f = parse_invoice_fields(text)
    assert f["invoice_date"] == "05/01/2019"


def test_empty_text_returns_blank_fields():
    f = parse_invoice_fields("")
    assert f["invoice_number"] is None
    assert f["invoice_date"] is None
    assert f["company_name"] is None
    assert f["total_amount"] is None
    assert f["field_completeness"] == 0.0
