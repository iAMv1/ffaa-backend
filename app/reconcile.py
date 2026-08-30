"""Match invoices bank lines. ponytail: O(n*m); amount-index if >5k rows."""
from rapidfuzz import fuzz


def _narr_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    # token_set_ratio handles word-order / extra bank junk better than ratio
    return fuzz.token_set_ratio(a.lower(), b.lower()) / 100.0


def _date_increment(invoice, bank_row, window: int) -> float | None:
    """0.2*closeness, or None when the date is outside the window (hard reject)."""
    if not (invoice.invoice_date and bank_row.date):
        return 0.0
    delta = abs((invoice.invoice_date - bank_row.date).days)
    if delta > window:
        return None
    return 0.2 * (1 - delta / (window + 1))


def _narr_contrib(invoice, bank_row) -> float:
    inv_no = (invoice.invoice_number or "").strip()
    narr = bank_row.narration or ""
    company = invoice.company_name or ""
    if inv_no and inv_no.lower() in narr.lower():
        return 0.25
    s = 0.15 * _narr_score(company, narr)
    if inv_no:
        s += 0.15 * (fuzz.partial_ratio(inv_no.lower(), narr.lower()) / 100.0)
    return s


def match_score(invoice, bank_row, date_window_days: int = 3) -> float:
    """0..1. Amount must match. Date window. RapidFuzz narration."""
    inv_amt = float(invoice.total_amount or 0)
    if inv_amt <= 0:
        return 0.0
    bank_amt = float(bank_row.credit or 0) or float(bank_row.debit or 0)
    if abs(bank_amt - inv_amt) > 0.05:
        return 0.0
    score = 0.5
    dinc = _date_increment(invoice, bank_row, date_window_days)
    if dinc is None:
        return 0.0
    score += dinc
    score += _narr_contrib(invoice, bank_row)
    return min(score, 1.0)


def best_matches(invoices, bank_rows, min_score: float = 0.55):
    used_inv, used_bank = set(), set()
    pairs = []
    for inv in invoices:
        for br in bank_rows:
            s = match_score(inv, br)
            if s >= min_score:
                pairs.append((s, inv, br))
    pairs.sort(key=lambda x: -x[0])
    out = []
    for s, inv, br in pairs:
        if inv.id in used_inv or br.id in used_bank:
            continue
        used_inv.add(inv.id)
        used_bank.add(br.id)
        out.append((inv, br, s))
    return out
