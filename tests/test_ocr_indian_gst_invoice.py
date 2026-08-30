"""Golden characterization test for a real Indian GST Tax Invoice.

Locks the extraction quality for born-digital PDFs (text-layer path) — the
layout that previously extracted only 2 of ~12 fields. The oracle text is the
exact PyMuPDF text layer of `0003 - Urvashi Sibal.pdf` so the test does not
depend on the file being present. Line-item extraction also runs against the
real PDF when available (skipped otherwise).
"""
import os

import pytest

from app.ocr import parse_invoice_fields, structured_line_items_from_pdf

_ORACLE = """Tax Invoice
Alok Misra & Co. (FY 2024-25) - (from 1-Apr-24)
AD-13, LGF, Tagore Garden
New Delhi
GSTIN/UIN: 07AAMFA0676L1Z5
State Name :  Delhi, Code : 07
E-Mail : caalokmisra@gmail.com
Buyer (Bill to)
Urvashi Sibal
Pocket-11, Sector-B, Vasant Kunj
SouthWest Delhi
GSTIN/UIN
: 07BJXPS9205G1ZJ
State Name
: Delhi, Code : 07
Invoice No.
0003/24-25
Dated
30-Apr-25
Sl
Particulars
Amount
per
Rate
HSN/SAC
No.
1
Individual Tax Preparation & Planning Services
3,850
998232
SGST Payable
347
%
9
CGST Payable
347
%
9
Total
4,544 ₹
Amount Chargeable (in words)
E. & O.E
Four Thousand Five Hundred Forty Four INR Only
HSN/SAC
Total
SGST/UTGST
CGST
Taxable
Tax Amount
Amount
Rate
Amount
Rate
Value
998232
694
347
9%
347
9%
3,850
Total
694
347
347
3,850
Tax Amount (in words)  : Six Hundred Ninety Four INR Only
Company's PAN
: AAMFA0676L
Declaration
We declare that this invoice shows the actual price of the
goods described and that all particulars are true and
correct.
Company's Bank Details
A/c Holder's Name : Alok Misra & Co
Bank Name
: HDFC BANK A/C
A/c No.
: 05512020000698
Branch & IFS Code: Tagore Garden & HDFC0002035
SWIFT Code
:
for Alok Misra & Co. (FY 2024-25) - (from 1-Apr-24)
Authorised Signatory
This is a Computer Generated Invoice"""


def test_indian_gst_invoice_header_fields():
    f = parse_invoice_fields(_ORACLE)
    assert f["invoice_number"] == "0003/24-25"
    # day-MONTH-year, NOT the "(from 1-Apr-24)" FY start date
    assert f["invoice_date"] == "30/04/25"
    # supplier name, FY/period suffix trimmed
    assert f["company_name"] == "Alok Misra & Co."
    assert f["supplier_gstin"] == "07AAMFA0676L1Z5"
    assert f["buyer_name"] == "Urvashi Sibal"
    assert f["buyer_gstin"] == "07BJXPS9205G1ZJ"
    assert f["place_of_supply"] == "07"
    # tax breakup from "X Payable <amt> % <rate>" lines
    assert f["gst_rate"] == 9.0
    assert f["cgst"] == 347.0
    assert f["sgst"] == 347.0
    assert f["igst"] is None
    # taxable derived from total minus tax (tax-inclusive Indian GST total)
    assert f["taxable_value"] == 3850.0
    assert f["total_amount"] == 4544.0
    assert f["hsn_code"] == "998232"
    assert f["total_in_words"] == "Four Thousand Five Hundred Forty Four INR Only"
    # must NOT grab the "Company's PAN" value as the supplier name
    assert "AAMFA0676L" not in (f["company_name"] or "")


def test_indian_gst_invoice_line_items():
    pdf = r"C:/Users/ItzP/Downloads/Documents/0003 - Urvashi Sibal.pdf"
    if not os.path.exists(pdf):
        pytest.skip("real invoice PDF not present in this environment")
    items = structured_line_items_from_pdf(pdf)
    assert items, "line-item extraction must not return a phantom row"
    row = items[0]
    assert row["desc"] == "Individual Tax Preparation & Planning Services"
    assert row["taxable"] == 3850.0
    assert row["hsn"] == 998232.0
    # the "No." header token must not leak as a data row
    assert all(it["desc"] != "No." for it in items)
