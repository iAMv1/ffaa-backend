"""Bank CSV + PDF. ponytail: PDF = pymupdf text first, EasyOCR page images if empty."""
import csv
import io
import os
import re
import tempfile
from datetime import datetime
from typing import Any

DATE_FMTS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%y")

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
    try:
        return float(s)
    except ValueError:
        return 0.0


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
            low = narr.lower()
            if any(x in low for x in ("dr", "withdraw", "debit", "atm", "upi out")):
                debit, credit, bal = a1, 0.0, a2
            else:
                debit, credit, bal = 0.0, a1, a2
        rows.append(
            {"date": d, "narration": narr, "debit": debit, "credit": credit, "balance": bal}
        )
    return rows


def _pdf_text(path: str) -> str:
    import fitz

    doc = fitz.open(path)
    parts = []
    for page in doc:
        parts.append(page.get_text("text"))
    doc.close()
    return "\n".join(parts)


def _pdf_ocr_text(path: str) -> str:
    """Render pages EasyOCR. ponytail: dpi 150; raise if bad scans."""
    import fitz
    from .ocr import reader

    doc = fitz.open(path)
    chunks = []
    tmp = tempfile.mkdtemp(prefix="bank_ocr_")
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
        img_path = os.path.join(tmp, f"p{i}.png")
        pix.save(img_path)
        chunks.append("\n".join(reader.readtext(img_path, detail=0)))
    doc.close()
    return "\n".join(chunks)


def parse_bank_pdf(path: str) -> list[dict[str, Any]]:
    text = _pdf_text(path)
    rows = parse_bank_text_lines(text)
    if rows:
        return rows
    # scanned PDF
    text = _pdf_ocr_text(path)
    return parse_bank_text_lines(text)


def parse_bank_file(filename: str, content: bytes) -> list[dict[str, Any]]:
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        return parse_bank_csv(content)
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
    return parse_bank_text_lines(content.decode("utf-8", errors="replace"))
