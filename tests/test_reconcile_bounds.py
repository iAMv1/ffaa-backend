"""Boundary tests for reconcile.match_score / best_matches.

Pins CURRENT behavior: ±0.05 amount tolerance, 3-day default date window
(duplicates.py uses a separate 7-day window — intentionally different),
narration scoring branches, mutual exclusion and min_score filtering.
"""
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.reconcile import best_matches, match_score


class FakeInvoice:
    def __init__(self, id=1, total_amount=1000.0, invoice_number="INV-2024-001",
                 invoice_date=date(2024, 5, 10), company_name=None):
        self.id = id
        self.total_amount = total_amount
        self.invoice_number = invoice_number
        self.invoice_date = invoice_date
        self.company_name = company_name


class FakeBankRow:
    def __init__(self, id=1, credit=1000.0, debit=0.0,
                 date=date(2024, 5, 10), narration="", ):
        self.id = id
        self.credit = credit
        self.debit = debit
        self.date = date
        self.narration = narration


def test_amount_within_exact_tolerance_passes():
    # exactly +0.05 over: passes the <= 0.05 guard
    inv = FakeInvoice(total_amount=1000.0)
    br = FakeBankRow(credit=1000.05)
    assert match_score(inv, br) > 0.0


def test_amount_minus_tolerance_passes():
    inv = FakeInvoice(total_amount=1000.0)
    br = FakeBankRow(credit=999.95)
    assert match_score(inv, br) > 0.0


@pytest.mark.parametrize("amt", [1000.06, 999.94])
def test_amount_outside_tolerance_zeroes(amt):
    inv = FakeInvoice(total_amount=1000.0)
    br = FakeBankRow(credit=amt)
    assert match_score(inv, br) == 0.0


def test_debit_side_used_when_credit_is_zero():
    inv = FakeInvoice(total_amount=500.0)
    br = FakeBankRow(credit=0.0, debit=500.0)
    assert match_score(inv, br) > 0.0


def test_zero_invoice_amount_zeroes():
    inv = FakeInvoice(total_amount=0.0)
    br = FakeBankRow(credit=0.0)
    assert match_score(inv, br) == 0.0


def test_date_window_default_three_days():
    inv = FakeInvoice(invoice_date=date(2024, 5, 10))
    # exactly 3 days apart: inside the window
    near = FakeBankRow(date=date(2024, 5, 13))
    s_near = match_score(inv, near)
    assert s_near > 0.0
    # 4 days apart: outside the DEFAULT window -> hard zero
    far = FakeBankRow(date=date(2024, 5, 14))
    assert match_score(inv, far) == 0.0


def test_date_window_score_peaks_at_same_day():
    inv = FakeInvoice(invoice_date=date(2024, 5, 10))
    same_day = match_score(inv, FakeBankRow(date=date(2024, 5, 10)))
    edge = match_score(inv, FakeBankRow(date=date(2024, 5, 13)))
    assert same_day > edge
    # full containment + same day: 0.5 base + 0.2 date + 0.25 inv-no = 0.95
    br = FakeBankRow(date=date(2024, 5, 10), narration="NEFT INV-2024-001 ACME")
    assert match_score(inv, br) == 0.95


def test_full_invoice_number_in_narration_beats_fuzzy_only():
    inv = FakeInvoice(invoice_number="INV-2024-001", company_name=None,
                      invoice_date=date(2024, 5, 10))
    full = match_score(inv, FakeBankRow(narration="payment for INV-2024-001 thanks"))
    partial = match_score(inv, FakeBankRow(narration="unrelated rent transfer"))
    assert full == 0.95  # exact containment bonus path
    assert partial < full


def test_missing_dates_skip_window_penalty():
    inv = FakeInvoice(invoice_date=None)
    br = FakeBankRow(date=None, narration="INV-2024-001")
    # no dates -> no date component; containment bonus still applies
    assert match_score(inv, br) == 0.75

def test_best_matches_min_score_filter():
    # no dates anywhere, empty narration: score is exactly the 0.5 base
    # (fuzzy branches contribute nothing) -> below default 0.55
    inv = FakeInvoice(id=1, invoice_number="ZZZ", company_name=None,
                      invoice_date=None)
    weak = FakeBankRow(id=10, date=None, narration="")
    assert match_score(inv, weak) == 0.5
    out = best_matches([inv], [weak])
    assert out == []
    # lowering min_score lets the bare-amount match through
    out_low = best_matches([inv], [weak], min_score=0.5)
    assert len(out_low) == 1


def test_best_matches_one_bank_row_cannot_serve_two_invoices():
    inv1 = FakeInvoice(id=1, total_amount=1000.0, invoice_number="INV-A")
    inv2 = FakeInvoice(id=2, total_amount=2000.0, invoice_number="INV-B")
    br1 = FakeBankRow(id=10, credit=1000.0, narration="pay INV-A")
    br2 = FakeBankRow(id=11, credit=2000.0, narration="pay INV-B")
    br3 = FakeBankRow(id=12, credit=2000.0, narration="pay INV-B again")
    out = best_matches([inv1, inv2], [br1, br2, br3])
    used_inv = [i.id for i, _, _ in out]
    used_bank = [b.id for _, b, _ in out]
    assert sorted(used_inv) == [1, 2]
    assert len(used_bank) == len(set(used_bank))  # no bank row reused
    assert len(out) == 2



def test_best_matches_picks_highest_scores_first():
    inv = FakeInvoice(id=1, invoice_number="INV-9", invoice_date=date(2024, 5, 10))
    good = FakeBankRow(id=10, narration="NEFT INV-9 salary")       # contains inv no
    ok = FakeBankRow(id=11, credit=1000.0, narration="random junk")  # fuzzy only
    out = best_matches([inv], [good, ok])
    assert len(out) == 1
    assert out[0][1].id == 10
