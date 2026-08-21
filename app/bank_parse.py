"""Bank CSV + PDF. ponytail: PDF = pymupdf text first, shared extract_text for scanned pages."""
import csv
import io
import os
import re
import shutil
import tempfile
from datetime import date, datetime
from typing import Any

from .ocr import extract_text

DATE_FMTS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%y")

HEADER_MAP = {
    "date": "date",
    "txndate": "date",
    "valuedate": "date",
    "narration": "narration",
    "description": "narration",
    "particulars": "narration",
    "remarks": "narration",
    "debit": "debit",
    "withdrawal": "debit",
    "withdrawals": "debit",
    "dr": "debit",
    "credit": "credit",
    "deposit": "credit",
    "deposits": "credit",
    "cr": "credit",
    "balance": "balance",
    "closingbalance": "balance",
}

# date ... amounts at end of line (bank stmt OCR/text)
# ponytail: loose line regex; fails exotic multi-currency layouts
LINE_RE = re.compile(
    r"(?P<date>\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2})"
    r"\s+(?P<narr>.+?)\s+"
    r"(?P<a1>[\d,]+\.?\d*)\s+(?P<a2>[\d,]+\.?\d*)(?:\s+(?P<a3>[\d,]+\.?\d*))?\s*$"
)


def _parse_date(s: str):
    s = (s or "").strip()
    for fmt in DATE_FMTS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _num(s) -> float:
    if s is None:
        return 0.0
    s = str(s).strip().replace(",", "").replace("₹", "").replace(" ", "")
    if not s or s == "-":
        return 0.0
    # SBI-style suffixed amounts: "1,234.56 Cr" / "1,234.56 DR"
    s = re.sub(r"(?:cr|dr)\.?$", "", s, flags=re.IGNORECASE)
    try:
        return float(s)
    except ValueError:
        return 0.0


# side detection by balance delta with relative tolerance (comma-stripped
# floats are ~never exactly equal to the rounded statement amounts)
def _delta_matches(prev: float, bal: float, amt: float) -> bool:
    return abs(abs(prev - bal) - amt) <= max(0.02, amt * 0.001)


# word-boundary keyword matching ("dr"/"cr"/"atm" must not match HYDRO/ADDRESS/...)
_DEBIT_KW_RE = re.compile(r"\b(dr|debit|withdraw|atm)\b")
_CREDIT_KW_RE = re.compile(r"\b(cr|credit|deposit|refund)\b")


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def parse_bank_csv(content: bytes | str) -> list[dict[str, Any]]:
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="replace")
    else:
        text = content
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return []

    col = {}
    for h in reader.fieldnames:
        key = HEADER_MAP.get(_norm_header(h))
        if key and key not in col:
            col[key] = h

    rows = []
    try:
        for raw in reader:
            d = _parse_date(raw.get(col.get("date", ""), ""))
            narr = (raw.get(col.get("narration", ""), "") or "").strip()
            debit = _num(raw.get(col.get("debit", ""), 0))
            credit = _num(raw.get(col.get("credit", ""), 0))
            bal = _num(raw.get(col.get("balance", ""), 0))
            if not d and not narr and not debit and not credit:
                continue
            rows.append(
                {"date": d, "narration": narr, "debit": debit, "credit": credit, "balance": bal}
            )
    except (csv.Error, ValueError):
        return []  # not actually CSV (binary/other format) — callers fall through
    return rows


def parse_bank_text_lines(text: str) -> list[dict[str, Any]]:
    """Parse free text lines date + narr + 2-3 money cols."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LINE_RE.search(line)
        if not m:
            continue
        d = _parse_date(m.group("date"))
        if not d:
            continue
        narr = m.group("narr").strip()
        a1, a2, a3 = _num(m.group("a1")), _num(m.group("a2")), _num(m.group("a3"))
        # 3 nums: debit credit balance OR credit debit balance — heuristic: last=balance
        if m.group("a3") is not None:
            debit, credit, bal = a1, a2, a3
            # if a1 looks like credit-only stmt (0 debit common): keep as-is
        else:
            # 2 nums: amount + balance; treat amount as credit if no DR marker
            if _DEBIT_KW_RE.search(narr.lower()):
                debit, credit, bal = a1, 0.0, a2
            else:
                debit, credit, bal = 0.0, a1, a2
        rows.append(
            {"date": d, "narration": narr, "debit": debit, "credit": credit, "balance": bal}
        )
    return rows


def parse_bank_text_block(text: str) -> list[dict[str, Any]]:
    """Parse Indian netbanking statement text: multi-line records.

    Row = date line + narration continuation lines + trailing money columns
    (HDFC: date, narration..., withdrawal, deposit, closing balance).
    Handles lakh-format commas (14,13,000.00) and word-broken PDF text.
    """
    rows: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    last_balance: float | None = None
    pending: list[float] = []

    # statement year for month-name dates without a year (RBC "15 Mar", Novus "09 Jan")
    MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    stmt_year = None
    m_year = re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+(\d{4})\b",
                       text, re.IGNORECASE)
    if m_year:
        stmt_year = int(m_year.group(1))
    m_year2 = re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*[-–]\s*\d{1,2},?\s+(\d{4})\b",
                        text, re.IGNORECASE)
    if m_year2:
        stmt_year = int(m_year2.group(1))
    if stmt_year is None:
        # last resort before now(): a 4-digit year adjacent to the FIRST
        # month-name date ("15 Mar 2024" / "2024 Mar 15") beats the current year
        m_first = re.search(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{1,2}",
            text, re.IGNORECASE)
        if m_first:
            ctx = text[max(0, m_first.start() - 30):m_first.end() + 30]
            m_y = re.search(r"\b((?:19|20)\d{2})\b", ctx)
            if m_y:
                stmt_year = int(m_y.group(1))
    if stmt_year is None:
        stmt_year = datetime.now().year

    def _day_mon(line: str):
        m = re.match(r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?$", line, re.IGNORECASE)
        if not m:
            return None
        try:
            return date(stmt_year, MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None  # impossible date (e.g. "31 Apr") from OCR noise

    def flush():
        nonlocal cur, last_balance, pending
        if cur:
            if cur.get("_dm") and pending:
                bal = pending[-1]
                amts = pending[:-1]
                cur["balance"] = bal
                if amts:
                    a = amts[-1]
                    if last_balance is not None and abs(last_balance - bal) >= abs(a) * 0.99:
                        if bal < last_balance:
                            cur["debit"] = a
                        else:
                            cur["credit"] = a
            cur["narration"] = re.sub(r"\s+", " ", cur["narration"]).strip()
            rows.append(cur)
            if cur["balance"] is not None:
                last_balance = cur["balance"]
        cur = None
        pending = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # ICICI: "1  02.04.2026  199.00  40718.24  narration..."
        m_icici = re.match(r"^(\d{1,3})\s+(\d{2}\.\d{2}\.\d{4})\s+([\d,]+\.\d{1,2})\s+([\d,]+\.\d{1,2})\s+(.*)$", line)
        if m_icici:
            flush()
            d = _parse_date(m_icici.group(2))
            if d:
                amt = _num(m_icici.group(3))
                bal = _num(m_icici.group(4))
                # 2 numbers: second is always balance; first is withdrawal OR deposit.
                # Decide side by balance delta vs previous row.
                if last_balance is not None and _delta_matches(last_balance, bal, amt):
                    debit, credit = (amt, 0.0) if bal < last_balance else (0.0, amt)
                else:
                    debit, credit = amt, 0.0
                cur = {"date": d, "narration": m_icici.group(5).strip(),
                       "debit": debit, "credit": credit, "balance": bal}
                last_balance = bal
            continue
        # SBI credit card: "26/12/2024 UPI/... 1,234.56 Cr" — exactly one amount
        # AND the line starts with a full date (not just any single-amount line)
        nums_count = len(re.findall(r"[\d,]+\.\d{1,2}", line))
        m_sbi = re.match(r"^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d,]+\.\d{1,2})\s*(Cr|DR)?\s*$", line)
        if nums_count == 1 and m_sbi:
            flush()
            d = _parse_date(m_sbi.group(1))
            if d:
                amt = _num(m_sbi.group(3))
                is_cr = (m_sbi.group(4) or "").upper() == "CR"
                cur = {"date": d, "narration": m_sbi.group(2).strip(),
                       "debit": 0.0 if is_cr else amt,
                       "credit": amt if is_cr else 0.0,
                       "balance": None}
            continue
        # month-name day-only date: "15 Mar", "09 Jan" (RBC, Novus, UK banks)
        dm = _day_mon(line)
        if dm is not None:
            if cur is not None:
                flush()
            cur = {"date": dm, "narration": "", "debit": 0.0, "credit": 0.0,
                   "balance": None, "_dm": True}
            continue
        m_date = re.match(r"^(\d{2}[/\-\.]\d{2}[/\-\.]\d{4})", line)
        if m_date:
            rest = line[m_date.end():].strip()
            nums = re.findall(r"[\d,]+\.\d{1,2}", rest)
            if cur is not None and cur["balance"] is None and not nums:
                # narration continuation that happens to start with a date (HDFC)
                cur["narration"] += " " + rest
                continue
            if cur is not None:
                flush()
            d = _parse_date(m_date.group(1))
            if not d:
                continue
            cur = {"date": d, "narration": re.sub(r"((?:[\d,]+\.\d{1,2}\s*)+)$", "", rest).strip(),
                   "debit": 0.0, "credit": 0.0, "balance": None}
            # date line may carry the amounts (HDFC/SBI style)
            if nums:
                vals = [_num(n) for n in nums]
                if len(vals) >= 3:
                    cur["debit"], cur["credit"], cur["balance"] = vals[-3], vals[-2], vals[-1]
                elif len(vals) == 2:
                    amt, bal = vals
                    low = cur["narration"].lower()
                    if _DEBIT_KW_RE.search(low):
                        cur["debit"] = amt
                    elif _CREDIT_KW_RE.search(low):
                        cur["credit"] = amt
                    elif last_balance is not None and _delta_matches(last_balance, bal, amt):
                        cur["debit"] = amt if bal < last_balance else 0.0
                        cur["credit"] = amt if bal > last_balance else 0.0
                    else:
                        cur["debit"] = amt
                    cur["balance"] = bal
                else:
                    cur["balance"] = vals[0]
            continue
        if cur is None:
            continue
        # money columns at line end: 1-3 amounts
        nums = re.findall(r"[\d,]+\.\d{1,2}", line)
        if nums and len(line) < 300:
            cur["narration"] += " " + re.sub(r"((?:[\d,]+\.\d{1,2}\s*)+)$", "", line).strip()
            vals = [_num(n) for n in nums]
            if cur.get("_dm") and len(vals) == 1:
                # month-name records (RBC/Novus): single values accumulate —
                # last is balance, earlier are amounts (side by delta at flush)
                pending.extend(vals)
            elif len(vals) >= 3:
                cur["debit"], cur["credit"], cur["balance"] = vals[-3], vals[-2], vals[-1]
            elif len(vals) == 2:
                amt, bal = vals
                low = cur["narration"].lower()
                if _DEBIT_KW_RE.search(low):
                    cur["debit"] = amt
                elif _CREDIT_KW_RE.search(low):
                    cur["credit"] = amt
                elif last_balance is not None and abs(abs(last_balance - bal) - amt) <= max(0.02, amt * 0.001):
                    cur["debit"] = amt if bal < last_balance else 0.0
                    cur["credit"] = amt if bal > last_balance else 0.0
                elif cur["balance"] is not None and bal != cur["balance"]:
                    cur["debit"] = amt if bal < cur["balance"] else 0.0
                    cur["credit"] = amt if bal > cur["balance"] else 0.0
                else:
                    cur["debit"] = amt
                cur["balance"] = bal
            elif len(vals) == 1:
                cur["balance"] = vals[0]
        else:
            cur["narration"] += " " + line
    flush()
    # strip column-header noise rows (numbers-only narrations)
    out = []
    for r in rows:
        narr = r["narration"]
        if re.match(r"^(Txn\s*Date|Date|Narration|Value\s*Date|Transaction\s*Date|Opening|Closing|Statement|Page|===)", narr, re.IGNORECASE):
            continue
        if len(narr) < 2 and not r["debit"] and not r["credit"]:
            continue
        out.append(r)
    return out


def _pdf_text(path: str) -> str:
    import fitz

    doc = fitz.open(path)
    parts = []
    for page in doc:
        parts.append(page.get_text("text"))
    doc.close()
    return "\n".join(parts)


def _pdf_words_clustered_text(path: str) -> str:
    """Reconstruct visual lines from word coordinates.

    Some Indian netbanking PDFs (SBI YONO etc) have word-broken text layers:
    get_text('text') returns column streams, not lines. Clustering words by y
    restores the visual rows the block parser expects.
    """
    import statistics

    import fitz

    doc = fitz.open(path)
    out = []
    for page in doc:
        words = page.get_text("words")  # x0,y0,x1,y1,word,...
        if not words:
            continue
        words.sort(key=lambda w: (w[1], w[0]))
        ys = [w[1] for w in words]
        gaps = [ys[i + 1] - ys[i] for i in range(len(ys) - 1) if ys[i + 1] > ys[i]]
        tol = max(4.0, 0.35 * (statistics.median(gaps) if gaps else 20.0))
        lines: list[list] = []
        for w in words:
            if not lines or w[1] - lines[-1][-1][1] > tol:
                lines.append([w])
            else:
                lines[-1].append(w)
        for line in lines:
            out.append(" ".join(w[4] for w in sorted(line, key=lambda w: w[0])))
    doc.close()
    return "\n".join(out)


def _pdf_ocr_text(path: str) -> str:
    """Render pages via PyMuPDF, extract text with shared OCR."""
    import fitz

    doc = fitz.open(path)
    chunks = []
    tmp = tempfile.mkdtemp(prefix="bank_ocr_")
    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
            img_path = os.path.join(tmp, f"p{i}.png")
            pix.save(img_path)
            chunks.append(extract_text(img_path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    doc.close()
    return "\n".join(chunks)


def parse_bank_pdf(path: str) -> list[dict[str, Any]]:
    text = _pdf_text(path)
    rows = parse_bank_text_block(text) or parse_bank_text_lines(text)
    if rows and any(r["debit"] or r["credit"] for r in rows):
        return rows
    # word-broken text layer (YONO-style column streams): naive text parses to
    # zero-amount rows; reconstruct visual lines from word coordinates instead
    rows = parse_bank_text_block(_pdf_words_clustered_text(path))
    if rows:
        return rows
    # scanned PDF
    text = _pdf_ocr_text(path)
    return parse_bank_text_lines(text)


def parse_bank_file(filename: str, content: bytes) -> list[dict[str, Any]]:
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        rows = parse_bank_csv(content)
        if rows:
            return rows
        return parse_bank_text_block(content.decode("utf-8", errors="replace"))
    if name.endswith(".pdf"):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            return parse_bank_pdf(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    # try csv anyway
    rows = parse_bank_csv(content)
    if rows:
        return rows
    return parse_bank_text_block(content.decode("utf-8", errors="replace"))
