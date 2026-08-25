"""TESTING ONLY — kirana-tuned parser v2 (total + supplier/buyer disambiguation).

Rules learned from the 45 kirana failures:
- Total: allow currency + space before value; require money-like value
  (2 decimals, or >=100); prefer GRAND TOTAL > line-start TOTAL > SUBTOTAL;
  never match 'Total' inside an item description (line-start anchor).
- Supplier: 'SOLD BY' / 'Bill From' / 'From' / 'Supplier' / 'M/S' label →
  the party on the following line. Buyer store names are ignored.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

CUR = r"(?:[₹$]|Rs\.?|RM\.?|INR|USD)?"


def _num(t):
    t = re.sub(r"[^0-9,.]", "", t or "").replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _moneyish(n, raw):
    """Money-like: 2-decimal raw token, or value >= 100."""
    if n is None:
        return False
    if re.search(r"\.\d{2}\b", raw):
        return True
    return n >= 100


def exp_total(text):
    lines = text.splitlines()
    # 1) GRAND TOTAL first (value same line or next 2; truncated OCR → above)
    for i, ln in enumerate(lines):
        if not re.search(r"GRAND\s*TOTAL", ln.upper()):
            continue
        for cand in lines[i:i + 3]:
            m = re.search(CUR + r"\s*([\d,]+\.\d{1,2})", cand)
            if m:
                n = _num(m.group(1))
                if n and n >= 100:
                    return n
        for cand in reversed(lines[max(0, i - 4):i]):
            m = re.search(r"([\d,]+\.\d{1,2})", cand)
            if m:
                n = _num(m.group(1))
                if n and n >= 100:
                    return n
    # 1b) in-words marker (Indian invoices)
    for i, ln in enumerate(lines):
        norm = re.sub(r"[^a-z]", "", ln.lower())
        if "inwords" in norm or "amountchargeable" in norm:
            for cand in reversed(lines[max(0, i - 3):i]):
                m = re.search(r"([\d,]+\.\d{1,2})", cand)
                if m:
                    n = _num(m.group(1))
                    if n and n >= 100:
                        return n
    # 2) line-start TOTAL (excludes 'Total' inside descriptions);
    #    multi-section bills have several — take the LAST match
    last = None
    for i, ln in enumerate(lines):
        up = ln.strip().upper()
        if re.match(r"^(?:TOTAL|NET\s*TOTAL|TOTAL\s*AMOUNT|TOTAL\s*DUE)\b", up):
            for cand in lines[i:i + 3]:
                m = re.search(CUR + r"\s*([\d,]+\.\d{1,2})", cand)
                if m:
                    n = _num(m.group(1))
                    if n and _moneyish(n, m.group(1)):
                        last = n
            if last is None:
                # truncated OCR: value printed ABOVE the label
                for cand in reversed(lines[max(0, i - 4):i]):
                    m = re.search(r"([\d,]+\.\d{1,2})", cand)
                    if m:
                        n = _num(m.group(1))
                        if n and n >= 100:
                            last = n
    if last is not None:
        return last
    # 3) SUBTOTAL fallback (before-GST total; better than nothing)
    for i, ln in enumerate(lines):
        if re.search(r"\bSUBTOTAL\b", ln.upper()) and not re.search(r"BEFORE GST", ln.upper()):
            for cand in lines[i:i + 3]:
                m = re.search(CUR + r"\s*([\d,]+\.\d{1,2})", cand)
                if m:
                    n = _num(m.group(1))
                    if n and n >= 100:
                        return n
    return None


def _is_name(s):
    if not s or len(s) < 3 or len(s) > 40:
        return False
    if s.endswith(":") and len(s) < 15:  # label-ish fragment
        return False
    if re.match(r"^[\d,]+\.?\d*$", s):
        return False
    if re.match(r"^[0-9A-Z]{10,}$", s.upper()):  # GSTIN / reference code
        return False
    if re.search(r"gstin|invoice|bill|date|purchase|buye|buyer", s.lower()):
        return False
    return True


def _paired_labels_supplier(lines):
    """Two-column bills: 'SOLD BY:'/'BILL TO:' labels adjacent, then two name
    lines — first is the buyer, second is the supplier."""
    sold = bill = None
    for i, ln in enumerate(lines[:12]):
        n = re.sub(r"[^a-z]", "", ln.lower())
        if "soldby" in n and sold is None:
            sold = i
        if ("billto" in n or n == "to") and bill is None:
            bill = i
        if "gstin" in n:
            break
    if sold is None or bill is None or abs(sold - bill) > 2:
        return None
    m = max(sold, bill)
    names = []
    for cand in lines[m + 1:m + 6]:
        s = cand.strip()
        if re.search(r"gstin", s.lower()) and len(names) >= 2:
            break
        if _is_name(s):
            names.append(s)
        if len(names) == 2:
            break
    return names[1] if len(names) >= 2 else (names[0] if names else None)


def exp_supplier(text):
    lines = text.splitlines()
    # two-column SOLD BY / BILL TO layout: supplier = second name
    paired = _paired_labels_supplier(lines)
    if paired:
        return paired
    for i, ln in enumerate(lines):
        n = re.sub(r"[^a-z0-9]", "", ln.lower())
        # label with same-line value: "From: Emami Limited", "Supplier: X"
        m = re.match(r"^[a-z .]+[:：]\s*(.+)$", ln, re.IGNORECASE)
        if m and re.match(r"^(from|supplier|vendor|soldby|billfrom|seller|ms)", n):
            v = m.group(1).strip()
            if _is_name(v) and not v.upper() in ("FROM", "SUPPLIER"):
                return v
        # supplier GSTIN marker (OCR typo-tolerant): value lines follow,
        # but STOP at the buyer block
        if re.search(r"suppli", n) and "gstin" in n:
            for cand in lines[i + 1:i + 6]:
                s = cand.strip()
                if re.search(r"buye|buyer|gstin", s.lower()):
                    break
                if re.match(r"^[0-9A-Z]{10,}$", s.upper()):
                    continue
                if _is_name(s):
                    return s
        # generic label, value on following lines
        if re.match(r"^(from|supplier|suppli|vendor|soldby|billfrom|seller|ms)", n):
            for cand in lines[i + 1:i + 4]:
                s = cand.strip()
                if re.search(r"buye|buyer|gstin", s.lower()):
                    break
                if _is_name(s) and not re.search(r"gstin", s.lower()):
                    return s
    # fallback: kirana bills often start with the supplier name
    for ln in lines[:6]:
        if _is_name(ln.strip()):
            return ln.strip()
    return None


def exp_date(text):
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{2}[/\-.]\d{2}[/\-.]\d{4})\b", text)
    return m.group(1) if m else None
