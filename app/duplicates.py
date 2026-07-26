from datetime import date
from typing import List, Tuple
from rapidfuzz import fuzz

from . import models

# ponytail: no fast-dedupe, no ML. RapidFuzz only.
SIMILARITY_THRESHOLD = 80.0
DATE_TOLERANCE_DAYS = 7


def _normalize(s: str | None) -> str:
    return (s or "").strip().lower().replace(" ", "")


def _field_score(a: str | None, b: str | None) -> float:
    return float(fuzz.ratio(_normalize(a), _normalize(b)))


def _amount_score(a: float | None, b: float | None) -> float:
    if a is None or b is None:
        return 0.0
    if a == 0 and b == 0:
        return 100.0
    if a == 0 or b == 0:
        return 0.0
    diff = abs(a - b)
    return max(0.0, 100.0 - (diff / max(abs(a), abs(b)) * 100.0))


def _date_score(a: date | None, b: date | None) -> float:
    if not a or not b:
        return 0.0
    delta = abs((a - b).days)
    if delta > DATE_TOLERANCE_DAYS:
        return 0.0
    return 100.0 - (delta / DATE_TOLERANCE_DAYS * 100.0)


def score_pair(inv_a: models.Invoice, inv_b: models.Invoice) -> Tuple[float, List[str]]:
    fields: List[str] = []

    number_score = _field_score(inv_a.invoice_number, inv_b.invoice_number)
    if number_score >= 90:
        fields.append("invoice_number")

    company_score = _field_score(inv_a.company_name, inv_b.company_name)
    if company_score >= 70:
        fields.append("company_name")

    amount_score = _amount_score(inv_a.total_amount, inv_b.total_amount)
    if amount_score >= 95:
        fields.append("total_amount")

    date_score = _date_score(inv_a.invoice_date, inv_b.invoice_date)
    if date_score >= 50:
        fields.append("invoice_date")

    # ponytail: simple weighted average. Weights: number 30%, amount 30%, company 20%, date 20%
    score = (
        number_score * 0.30
        + amount_score * 0.30
        + company_score * 0.20
        + date_score * 0.20
    )
    return score, fields


def find_duplicates(invoice: models.Invoice, candidates: List[models.Invoice]) -> List[Tuple[models.Invoice, float, str]]:
    matches: List[Tuple[models.Invoice, float, str]] = []
    for other in candidates:
        if other.id == invoice.id:
            continue
        if other.is_duplicate:  # type: ignore[attr-defined]
            continue
        score, fields = score_pair(invoice, other)
        if score >= SIMILARITY_THRESHOLD:
            matches.append((other, score, ",".join(fields)))
    return sorted(matches, key=lambda x: x[1], reverse=True)
