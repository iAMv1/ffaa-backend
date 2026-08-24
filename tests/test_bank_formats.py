"""Bank format matrix for bank_parse.parse_bank_file.

Characterizes CURRENT behavior: CSV column mapping, HDFC/SBI-Cr/ICICI-style
fixed-format texts, balance-delta side detection, lakh-comma amounts and
month-name dates with inferred statement year.
"""
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.bank_parse import _delta_matches, parse_bank_file


# --- CSV ----------------------------------------------------------------------

CSV_CONTENT = (
    "Date,Narration,Debit,Credit,Balance\n"
    "01/04/2024,UPI/RENT PAID,12000.50,0.00,45123.75\n"
    '02/04/2024,LAKH RTGS IN,0.00,"1,23,456.78",168579.53\n'
    "03/04/2024,CARD PAYMENT DR,999.99,0.00,167579.54\n"
)


def test_csv_rows_sides_and_lakh_commas():
    rows = parse_bank_file("statement.csv", CSV_CONTENT.encode("utf-8"))
    assert len(rows) == 3
    r1, r2, r3 = rows
    assert r1["date"] == date(2024, 4, 1)
    assert r1["narration"] == "UPI/RENT PAID"
    assert r1["debit"] == 12000.50
    assert r1["credit"] == 0.0
    assert r1["balance"] == 45123.75
    # lakh-comma amount survives the quoted CSV cell
    assert r2["credit"] == 123456.78
    assert r2["debit"] == 0.0
    assert r3["debit"] == 999.99


def test_csv_bytes_with_bom():
    rows = parse_bank_file("a.csv", b"\xef\xbb\xbf" + CSV_CONTENT.encode("utf-8"))
    assert len(rows) == 3


# --- HDFC-style fixed-format text (block parser) -------------------------------

HDFC_TEXT = (
    "HDFC BANK STATEMENT\n"
    "01/04/2024 UPI/DR RENT 12,000.00 45,000.00\n"
    "02/04/2024 NEFT CREDIT SALARY 40,000.00 85,000.00\n"
    "05/04/2024 RTGS BIGVENDOR 1,23,456.78 2,08,456.78\n"
)


def test_hdfc_style_text_block():
    rows = parse_bank_file("hdfc_stmt.txt", HDFC_TEXT.encode("utf-8"))
    assert len(rows) == 3
    # narration keyword DR -> debit side
    assert rows[0]["debit"] == 12000.00
    assert rows[0]["credit"] == 0.0
    # narration keyword CREDIT -> credit side
    assert rows[1]["credit"] == 40000.00
    assert rows[1]["debit"] == 0.0
    # no keyword: balance delta |85,000 - 2,08,456.78| == 1,23,456.78,
    # balance rose -> credit; lakh commas parsed
    assert rows[2]["credit"] == 123456.78
    assert rows[2]["balance"] == 208456.78


# --- SBI credit-card style: single amount + Cr/DR suffix -----------------------

SBI_TEXT = (
    "26/12/2024 UPI/CR/PAYBACK 1,234.56 Cr\n"
    "27/12/2024 UPI/DEBIT/MERCHANT 780.00 DR\n"
)


@pytest.mark.parametrize(
    ("line_idx", "side_field", "amount"),
    [(0, "credit", 1234.56), (1, "debit", 780.00)],
)
def test_sbi_credit_card_cr_suffix(line_idx, side_field, amount):
    rows = parse_bank_file("sbi_cc.txt", SBI_TEXT.encode("utf-8"))
    assert len(rows) == 2
    row = rows[line_idx]
    other = "debit" if side_field == "credit" else "credit"
    assert row[side_field] == amount
    assert row[other] == 0.0
    assert row["balance"] is None


# --- ICICI-style: srno date amount balance narration ---------------------------

ICICI_TEXT = (
    "Sr No Date Amount Balance Narration\n"
    "1 02.04.2026 199.00 40718.24 UPI PAYER1\n"
    "2 03.04.2026 500.00 41218.24 NEFT CREDIT\n"
    "3 04.04.2026 300.00 40918.24 BILLPAY DEBIT\n"
)


def test_icici_style_balance_delta_side_detection():
    rows = parse_bank_file("icici.txt", ICICI_TEXT.encode("utf-8"))
    assert len(rows) == 3
    # first row has no previous balance -> defaults to debit
    assert rows[0]["debit"] == 199.00
    assert rows[0]["balance"] == 40718.24
    # balance rose by exactly the amount -> credit
    assert rows[1]["credit"] == 500.00
    assert rows[1]["debit"] == 0.0
    # balance fell by exactly the amount -> debit
    assert rows[2]["debit"] == 300.00
    assert rows[2]["credit"] == 0.0


# --- RBC-style month-name dates with inferred year ------------------------------

RBC_TEXT = (
    "RBC Statement 01 Jan 2024 to 29 Feb 2024\n"
    "10 Jan\n"
    "FIRST ROW OPENING\n"
    "100.00\n"
    "11 Jan\n"
    "DEPOSIT PAYROLL\n"
    "500.00\n"
    "600.00\n"
    "12 Jan\n"
    "ATM WITHDRAWAL\n"
    "200.00\n"
    "400.00\n"
)


def test_rbc_month_name_dates_inferred_year_and_delta_side():
    rows = parse_bank_file("rbc.txt", RBC_TEXT.encode("utf-8"))
    assert len(rows) == 3
    assert all(r["date"].year == 2024 for r in rows)
    assert rows[0]["date"] == date(2024, 1, 10)
    # opening row: single pending value becomes balance, no side assigned
    assert rows[0]["balance"] == 100.0
    assert rows[0]["debit"] == 0.0 and rows[0]["credit"] == 0.0
    # accumulated singles: last is balance, earlier is amount;
    # delta vs previous balance decides side
    assert rows[1]["credit"] == 500.0
    assert rows[1]["balance"] == 600.0
    assert rows[2]["debit"] == 200.0
    assert rows[2]["balance"] == 400.0


# --- balance-delta tolerance helper --------------------------------------------


def test_delta_matches_relative_tolerance():
    # exact
    assert _delta_matches(1000.0, 900.0, 100.0)
    # within max(0.02, amt*0.001)
    assert _delta_matches(1000.0, 900.03, 100.02)
    # outside tolerance
    assert not _delta_matches(1000.0, 900.5, 100.0)
    # small amounts floor at 0.02 absolute
    assert _delta_matches(10.0, 9.98, 0.01)
    assert not _delta_matches(10.0, 9.90, 0.01)


# --- unknown extension falls through to text parsing ----------------------------


def test_unknown_extension_still_parses_text_block():
    rows = parse_bank_file("stmt.dat", HDFC_TEXT.encode("utf-8"))
    assert len(rows) == 3
