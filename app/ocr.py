"""OCR engine: RapidOCR (ONNX INT8) primary + PaddleOCR medium fallback.
PDF text-layer fast path first. Shared extract_text for invoices + bank."""
import os
import re
import tempfile
import uuid
from typing import Any, Dict

import cv2
import fitz

os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_use_mkldnn", "0")

_OCR = None
_RAPID = None
MAX_PDF_PAGES = 20
PDF_DPI = 200


def _get_ocr_engine():
    global _OCR
    if _OCR is None:
        from paddleocr import PaddleOCR

        # accuracy fallback only (rapid is primary): no-unwarp + batch16
        _OCR = PaddleOCR(
            lang="en",
            use_doc_unwarping=False,
            rec_batch_num=16,
        )
    return _OCR


def _get_rapid_engine():
    """RapidOCR (PP-OCRv4 INT8 ONNX). Measured: 3.4s receipt vs 68.6s paddle
    medium; more accurate on dense Indian invoices (kirana 41/50 vs 0/50
    totals with our parser). NOTE: no binarize — INT8 models degrade on it."""
    global _RAPID
    if _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR

        _RAPID = RapidOCR()
    return _RAPID


def _rapid_words(image_path: str):
    """RapidOCR result → [(text, x0, y0)]. Returns (words, ok)."""
    result, _ = _get_rapid_engine()(image_path)
    if not result:
        return [], False
    words = [(ln[1], ln[0][0][0], ln[0][0][1]) for ln in result]
    words.sort(key=lambda w: (w[2], w[1]))
    return words, True


def extract_text(image_path: str) -> str:
    return extract_text_and_items(image_path)[0]


def document_to_images(path: str) -> tuple[list[str], str | None]:
    """PDF→page JPG paths; image→[path]. Returns (paths, warning)."""
    ext = os.path.splitext(path)[1].lower()
    if ext != ".pdf":
        return [path], None
    doc = fitz.open(path)
    img_paths: list[str] = []
    warn = None
    tmpdir = tempfile.mkdtemp(prefix="inv_ocr_")
    n = len(doc)
    limit = min(n, MAX_PDF_PAGES)
    if n > MAX_PDF_PAGES:
        warn = f"OCR stopped after {MAX_PDF_PAGES} of {n} pages"
    scale = PDF_DPI / 72
    for i in range(limit):
        pix = doc[i].get_pixmap(matrix=fitz.Matrix(scale, scale))
        img_path = os.path.join(tmpdir, f"p{i}.jpg")
        pix.save(img_path, jpg_quality=85)
        img_paths.append(img_path)
    doc.close()
    return img_paths, warn


MAX_IMAGE_SIDE = 2200
MIN_IMAGE_SIDE = 1200


def _cap_size(img):
    """Downscale only very large images; upscale small ones. Small digits in
    grid tables vanish below ~1600px height, so keep native res as long as
    possible (det is the cost, rec quality is the win)."""
    h, w = img.shape[:2]
    if max(h, w) > MAX_IMAGE_SIDE:
        scale = MAX_IMAGE_SIDE / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    elif max(h, w) < MIN_IMAGE_SIDE:
        scale = MIN_IMAGE_SIDE / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    return img


def preprocess_image(image_path: str) -> str:
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Could not read image/PDF: {image_path}")
    img = _cap_size(img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # cheap noise removal — fastNlMeans was the main latency hog
    if gray.std() < 12:  # noisy image → light median
        gray = cv2.medianBlur(gray, 3)

    # binarize always: measurement showed grayscale loses small digits in
    # grid tables (row qty/rate/amount cells); CLAHE evens lighting first
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    processed_path = os.path.join(
        tempfile.gettempdir(), f"ffaa_{os.getpid()}_{uuid.uuid4().hex}.jpg"
    )
    cv2.imwrite(processed_path, gray)
    return processed_path


def _safe_float(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def _fix_digits(s: str) -> str:
    """Fix OCR char confusions inside numeric tokens: O->0, l|I->1, S->5, B->8."""
    out = []
    for ch in s:
        if ch in "Oo":
            out.append("0")
        elif ch in "lI|":
            out.append("1")
        elif ch == "S":
            out.append("5")
        elif ch == "B":
            out.append("8")
        else:
            out.append(ch)
    return "".join(out)


def _num(text: str) -> float | None:
    """Parse money string: strip currency/commas/apostrophes, fix digit confusions."""
    t0 = str(text).strip()
    if not t0:
        return None
    # not a number if it contains letters beyond the OCR-confusion set
    if re.search(r"[A-Za-z]", t0) and re.search(r"[^0-9OoIlSB,.\-'']", t0):
        return None
    t = re.sub(r"[^0-9OoIlSB,.\-'']", "", t0)
    t = _fix_digits(t)
    t = t.replace(",", "").replace("'", "")
    try:
        return float(t)
    except ValueError:
        return None


LABEL_LINES = {
    "SHIP TO", "BILL TO", "SOLD TO", "INVOICE TO", "CUSTOMER", "INSTRUCTIONS",
    "REMIT TO", "SERVICE ADDRESS", "BILLING ADDRESS", "NAME", "ADDRESS",
}


def parse_line_items(text: str) -> list[dict]:
    """Extract table rows: <desc> <qty> <rate> <amount>. Returns list of dicts."""
    items = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if "item description" in low or "particulars" in low or "description" in low and "unit price" in low:
            in_table = True
            continue
        if "taxable value" in low or "grand total" in low or "subtotal" in low or "total" in low:
            in_table = False
            continue
        if not in_table:
            continue
        if len(line) < 6 or line.isupper() and len(line) < 20:
            continue  # column header / junk line
        lead = None
        rest = line
        m0 = re.match(r"^(\d+(?:\.\d+)?)\s+(.+)$", line)
        if m0:
            lead, rest = m0.group(1), m0.group(2)
        m = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+([0-9,'.]+\.?\d*)\s*$", rest)
        if not m:
            m = re.match(r"^(.*?)\s+([0-9,'.]+\.?\d*)\s*$", rest)
        if m:
            desc = m.group(1).strip()
            nums = [v for v in (_safe_float(x.replace(",", "").replace("'", ""))
                                for x in m.groups()[1:]
                                if re.fullmatch(r"[0-9,'.]+\.?\d*", x))
                    if v is not None]
            if not nums:
                continue
            if not desc or len(desc) < 3:
                continue
            if re.match(r"^(Quantity|Qty|Rate|Price|Amount|Total|HSN|Item|Description|Unit)[: ]*$", desc, re.IGNORECASE):
                continue  # column label line, not a real item
            if lead is not None:
                qty = float(lead)
                # qty==1 and first num ≈ amount → lead was a position number, drop it
                if len(nums) >= 2 and qty == 1 and abs(nums[0] * 1.0 - nums[-1]) / max(nums[-1], 1) < 0.02:
                    qty = None
            else:
                # no lead: 3 nums = (qty, rate, amount); fewer = (rate?, amount) guess
                qty = nums[0] if len(nums) == 3 else None
            rate = None
            taxable = nums[-1]
            if len(nums) >= 2:
                rate = nums[-2]
            if qty is None and len(nums) >= 2:
                qty = nums[-2]
            if qty is not None and rate is not None and abs(qty * rate - taxable) / max(taxable, 1) > 0.05:
                qty = None  # numbers don't multiply out — treat as (qty?, amount) guess
            items.append({"desc": desc, "qty": qty, "rate": rate, "taxable": taxable, "hsn": None})
    return items


def _items_from_word_lines(lines) -> list[dict]:
    """Zone-based table extraction from clustered word lines (text, x0, y0)."""
    items: list[dict] = []
    header_idx = None
    for li, line in enumerate(lines):
        joined = " ".join(w[0] for w in line).upper()
        if (re.search(r"DESC|ITEM|PARTICULAR|PRODUCT", joined)
                and re.search(r"QTY|QUANT|PRICE|RATE|AMOUNT|TOTAL", joined)):
            header_idx = li
            break
    if header_idx is None:
        return items

    zones: list[tuple[str, float]] = []  # (kind, x0) in header x-order
    for w in lines[header_idx]:
        up = w[0].upper()
        if re.match(r"QTY|QUANT", up):
            zones.append(("qty", w[1]))
        elif re.match(r"RATE|UNIT", up) or up.startswith("PRICE"):
            zones.append(("rate", w[1]))
        elif re.match(r"AMOUNT|TOTAL", up):
            zones.append(("amount", w[1]))
        elif re.match(r"DESC|ITEM|PARTICULAR", up):
            zones.append(("desc", w[1]))
    zones.sort(key=lambda z: z[1])
    kinds = [z[0] for z in zones]
    if "desc" not in kinds or not any(k in ("rate", "amount", "qty") for k in kinds):
        return items

    def zone_of(x0):
        best = None
        for kind, zx in zones:
            if x0 >= zx - 2.0:
                best = kind
            else:
                break
        return best or ("desc" if kinds[0] == "desc" else None)

    def first_num(ws):
        for w in sorted(ws, key=lambda w: w[1]):
            n = _num(w[0])
            if n is not None:
                return n
        return None

    for line in lines[header_idx + 1:]:
        joined = " ".join(w[0] for w in line).upper()
        if re.search(r"SUBTOTAL|TOTAL|TAX|BALANCE|GRAND", joined):
            break
        by_zone: dict[str, list] = {}
        for w in line:
            by_zone.setdefault(zone_of(w[1]), []).append(w)
        desc = " ".join(w[0] for w in by_zone.get("desc", []) if _num(w[0]) is None)
        desc = re.sub(r"^\d+\s+", "", desc).strip()
        qty = first_num(by_zone.get("qty", []))
        rate = first_num(by_zone.get("rate", []))
        amount = first_num(by_zone.get("amount", []))
        if amount is None:
            for w in reversed(line):
                n = _num(w[0])
                if n is not None:
                    amount = n
                    break
        if not desc and amount is None:
            continue
        if len(desc) < 2 and amount is None:
            continue
        if qty == 1 and rate is not None and amount is not None \
                and abs(rate * 1.0 - amount) / max(amount, 1) < 0.02:
            qty = None  # position number, not qty
        items.append({"desc": desc or "—", "qty": qty, "rate": rate,
                      "taxable": amount, "hsn": None})
    return items


def _cluster_lines(words, tol_factor=0.35):
    """Cluster (text, x0, y0) words into lines by y.

    Tolerance derives from the median line gap (robust to box-height scale).
    """
    import statistics

    words = sorted(words, key=lambda w: (w[2], w[1]))
    ys = [w[2] for w in words]
    gaps = [ys[i + 1] - ys[i] for i in range(len(ys) - 1) if ys[i + 1] > ys[i]]
    median_gap = statistics.median(gaps) if gaps else 20.0
    tol = max(4.0, tol_factor * median_gap)
    lines: list[list] = []
    for w in words:
        if not lines or w[2] - lines[-1][-1][2] > tol:
            lines.append([w])
        else:
            lines[-1].append(w)
    return lines


def structured_line_items_from_pdf(file_path: str) -> list[dict]:
    """Column-aware line items from a PDF text layer via word coordinates."""
    doc = fitz.open(file_path)
    items: list[dict] = []
    try:
        for p in range(min(len(doc), MAX_PDF_PAGES)):
            words = doc[p].get_text("words")
            if not words:
                continue
            wl = [(w[4], w[0], w[1]) for w in words]
            items.extend(_items_from_word_lines(_cluster_lines(wl)))
    finally:
        doc.close()
    return items


def _ocr_words(result) -> list[tuple[str, float, float]]:
    """(text, x0, y0) lines from PaddleOCR result objects (rec_texts + boxes)."""
    out = []
    for r in result:
        texts = getattr(r, "rec_texts", None)
        if not texts and isinstance(r, dict):
            texts = r.get("rec_texts")
        if not texts:
            continue
        polys = getattr(r, "rec_polys", None)
        if not polys and isinstance(r, dict):
            polys = r.get("rec_polys")
        boxes = getattr(r, "rec_boxes", None)
        if not boxes and isinstance(r, dict):
            boxes = r.get("rec_boxes")
        for i, t in enumerate(texts):
            x = y = 0.0
            if i < len(polys) and polys[i] is not None:
                x = min(pt[0] for pt in polys[i])
                y = min(pt[1] for pt in polys[i])
            elif i < len(boxes) and boxes[i] is not None:
                # rec_boxes are flat [x1, y1, x2, y2]; anything else → origin
                try:
                    b = boxes[i]
                    x, y = float(b[0]), float(b[1])
                except Exception:
                    x = y = 0.0
            out.append((str(t), x, y))
    return out


def extract_text_and_items(image_path: str) -> tuple[str, list[dict]]:
    """RapidOCR primary (raw image — no binarize, it degrades INT8 models);
    paddle medium fallback when detection is thin or totals look wrong.
    One OCR pass for both text and line items."""
    words, ok = _rapid_words(image_path)
    text = "\n".join(w[0] for w in words)

    def _needs_fallback(txt):
        f = parse_invoice_fields(txt)
        total = f.get("total_amount")
        taxable = f.get("taxable_value")
        rate = f.get("gst_rate")
        if not ok:
            return True
        if len(words) < 25 and total is None:
            return True  # thin detection AND no total → weak read, rerun accurate
        if total is None:
            # weak total line: if the doc clearly has a total, rerun accurate engine
            if re.search(r"\b(TOTAL|GRAND TOTAL|AMOUNT DUE|TOTAL AMOUNT)\b", txt.upper()):
                return True
            return False
        if rate and rate > 0 and taxable and taxable > 0 and abs(total - taxable) / taxable < 0.01:
            return True  # total should exceed taxable when GST applies
        return False

    if _needs_fallback(text):
        engine = _get_ocr_engine()
        result = engine.predict(preprocess_image(image_path))
        # paddle fallback may return None/empty on unreadable input — keep rapid read
        fb_words = _ocr_words(result) if result else []
        if fb_words:
            words = fb_words
            text = "\n".join(w[0] for w in words)
    items = _items_from_word_lines(_cluster_lines(words))
    return text, items


def parse_invoice_fields(text: str) -> Dict[str, Any]:
    fields = {
        "invoice_number": None,
        "invoice_date": None,
        "company_name": None,
        "gst_rate": None,
        "taxable_value": None,
        "total_amount": None,
        "cgst": None,
        "sgst": None,
        "igst": None,
        "hsn_code": None,
        "quantity": None,
        "item_description": None,
        "confidence": 0.0,
    }
    lines = text.splitlines()

    inv_match = re.search(
        r"(?:Invoice\s*(?:No\.?|Number))\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/ .]+)",
        text,
        re.IGNORECASE,
    )
    if inv_match:
        inv = re.sub(r"\s+", "", inv_match.group(1).strip())
        # char class absorbs the next label ("INV-01 Dated" → "INV-01Dated"):
        # cut at date-label words, case-insensitive
        inv = re.split(r"(?:dated|date|dt)", inv, flags=re.IGNORECASE)[0].strip()
        fields["invoice_number"] = inv or None

    # company/supplier — v2: label-first (From/Sold By/Supplier GSTIN) then
    # legacy label-walk, then all-caps fallback. Disambiguates buyer vs vendor
    # on purchase bills (kirana measured 36/50 vs 7/50 legacy).
    def _is_vendor_name(s):
        if not s or len(s) < 3 or len(s) > 40:
            return False
        if s.endswith(":") and len(s) < 15:
            return False
        if re.match(r"^[\d,]+\.?\d*$", s):
            return False
        if re.match(r"^[0-9A-Z]{10,}$", s.upper()):
            return False
        if re.search(r"gstin|invoice|bill|date|purchase|buye|buyer", s.lower()):
            return False
        return True

    # two-column SOLD BY / BILL TO bills: labels adjacent, then two name
    # lines — first is the buyer, second is the supplier
    sold = bill = None
    for i, ln in enumerate(lines[:12]):
        n2 = re.sub(r"[^a-z]", "", ln.lower())
        if "soldby" in n2 and sold is None:
            sold = i
        if ("billto" in n2 or n2 == "to") and bill is None:
            bill = i
        if "gstin" in n2:
            break
    if sold is not None and bill is not None and abs(sold - bill) <= 2:
        m_idx = max(sold, bill)
        names = []
        for cand in lines[m_idx + 1:m_idx + 6]:
            s = cand.strip()
            if re.search(r"gstin", s.lower()) and len(names) >= 2:
                break
            if _is_vendor_name(s):
                names.append(s)
            if len(names) == 2:
                break
        if len(names) >= 2:
            fields["company_name"] = names[1]

    for i, ln in enumerate(lines):
        if fields["company_name"]:
            break
        n = re.sub(r"[^a-z0-9]", "", ln.lower())
        m = re.match(r"^[a-z .]+[:：]\s*(.+)$", ln, re.IGNORECASE)
        if m and re.match(r"^(from|supplier|vendor|soldby|billfrom|seller|ms|company|name|party)", n):
            v = m.group(1).strip()
            if _is_vendor_name(v) and v.upper() not in ("FROM", "SUPPLIER", "COMPANY", "NAME", "PARTY"):
                fields["company_name"] = v
                break
        if re.search(r"suppli", n) and "gstin" in n:
            for cand in lines[i + 1:i + 6]:
                s = cand.strip()
                if re.search(r"buye|buyer|gstin", s.lower()):
                    break
                if re.match(r"^[0-9A-Z]{10,}$", s.upper()):
                    continue
                if _is_vendor_name(s):
                    fields["company_name"] = s
                    break
            if fields["company_name"]:
                break
        if re.match(r"^(from|supplier|suppli|vendor|soldby|billfrom|seller|ms|company|name|party)", n):
            for cand in lines[i + 1:i + 4]:
                s = cand.strip()
                if re.search(r"buye|buyer|gstin", s.lower()):
                    break
                if _is_vendor_name(s):
                    fields["company_name"] = s
                    break
            if fields["company_name"]:
                break
    if not fields["company_name"]:
        for ln in lines[:6]:
            if _is_vendor_name(ln.strip()):
                fields["company_name"] = ln.strip()
                break
    if not fields["company_name"]:
        company_match = re.search(
            r"(?:Company|Vendor|Supplier|Biller|Party|Bill\s*To|Invoice\s*To|Sold\s*To|Customer)\s*(?:Name)?(?:\s*[:\-]\s*|\s*\n\s*)(.*)",
            text,
            re.IGNORECASE,
        )
        if company_match:
            # label may be followed by other label lines — walk forward to first real value
            idx = company_match.end()
            candidates = [company_match.group(1)] + [ln for ln in text[idx:].splitlines()]
            for cand0 in candidates:
                cand = cand0.strip().strip(":").strip()
                if not cand or len(cand) < 3 or len(cand) > 40:
                    continue
                if ":" in cand or "fabricat" in cand.lower() or "generated" in cand.lower():
                    continue
                if cand.startswith(("CID-", "PO-", "CUST-")):
                    continue
                if cand.upper() in LABEL_LINES or cand.upper().split(" ")[0] in LABEL_LINES:
                    continue
                fields["company_name"] = cand
                break
    if not fields["company_name"]:
        # label-less fallback (receipts): first all-caps line that reads like a
        # vendor name — ≥2 words, has letters, not a document label
        for line in text.splitlines():
            s = line.strip()
            if not s or not s.isupper():
                continue
            words = s.split()
            if len(words) < 2 or len(s) < 8 or len(s) > 40:
                continue
            if not any(c.isalpha() for c in s):
                continue
            if any(k in s for k in ("TAX INVOICE", "GSTIN", "RECEIPT", "INVOICE NO", "DATE", "GST ID", "CO REG", "TEL", "PHONE", "EMAIL", "WEB", "ADDRESS", "BILL TO", "SHIP TO")):
                continue
            fields["company_name"] = s
            break

    date_match = re.search(
        r"(?:Date|Dt\.?|Invoice\s*Date)\s*[:\-]?\s*\n?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2})",
        text,
        re.IGNORECASE,
    )
    if date_match:
        fields["invoice_date"] = date_match.group(1).strip()
    else:
        # bare-date fallback: first standalone dd/mm/yyyy not inside a range/period line
        for line in text.splitlines():
            if re.search(r"period|range|until|\bto\b", line, re.IGNORECASE):
                continue
            m = re.search(
                r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b", line
            )
            if m and line.count(m.group(1)) == 1 and line.count("/") + line.count("-") <= 2:
                fields["invoice_date"] = m.group(1).strip()
                break
    if not fields["invoice_date"]:
        # month-name date: "JAN 5, 2019" / "January 5, 2019"
        MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                  "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        m = re.search(
            r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b",
            text,
            re.IGNORECASE,
        )
        if m:
            mm = MONTHS[m.group(1)[:3].lower()]
            fields["invoice_date"] = f"{int(m.group(2)):02d}/{mm:02d}/{m.group(3)}"

    # total — v2 rules (measured: 41/50 kirana vs 5/50 legacy):
    # 1) GRAND TOTAL (merged-word tolerant) with value same/next line or above
    # 2) "in words" marker (Indian invoices) — grand total sits above it
    # 3) line-start TOTAL (last match; multi-section bills) + truncation fallback
    # 4) SUBTOTAL (pre-GST) fallback
    # 5) legacy regex as last resort

    def _tot_cands(lines, i, above=False):
        if above:
            return reversed(lines[max(0, i - 4):i])
        return lines[i:i + 3]

    def _grab(lines, i, cur_re, above=False):
        for cand in _tot_cands(lines, i, above):
            m = re.search(cur_re + r"\s*([\d,]+(?:\.\d{1,2})?)", cand)
            if m:
                n = _num(m.group(1))
                if n and n >= 100:
                    return n
        return None

    cur = r"(?:[₹$]|Rs\.?|RM\.?|INR|USD)?"
    total = None
    for i, ln in enumerate(lines):
        if not re.search(r"GRAND\s*TOTAL", ln.upper()):
            continue
        total = _grab(lines, i, cur) or _grab(lines, i, cur, above=True)
        if total:
            break
    if total is None:
        for i, ln in enumerate(lines):
            norm = re.sub(r"[^a-z]", "", ln.lower())
            if "inwords" in norm or "amountchargeable" in norm:
                total = _grab(lines, i, cur, above=True)
                if total:
                    break
    if total is None:
        last = None
        for i, ln in enumerate(lines):
            up = ln.strip().upper()
            if re.match(r"^(?:TOTAL|NET\s*TOTAL|TOTAL\s*AMOUNT|TOTAL\s*DUE)\b", up):
                v = _grab(lines, i, cur)
                if v is None:
                    v = _grab(lines, i, cur, above=True)
                if v is not None:
                    last = v
        total = last
    if total is None:
        for i, ln in enumerate(lines):
            if re.search(r"\bSUBTOTAL\b", ln.upper()) and not re.search(r"BEFORE GST", ln.upper()):
                total = _grab(lines, i, cur)
                if total:
                    break
    if total is None:
        legacy = None
        for m in re.finditer(
            r"(?<!SUB)(Total|Grand\s*Total|Total\s*Due)\s*(?:Amount)?\s*[:\-]?\s*\n?\s*[₹$]?\s*([0-9OoIlSB,'.]+)",
            text,
            re.IGNORECASE,
        ):
            v = _num(m.group(2))
            if v and v >= 100:
                legacy = v  # last ≥100 match wins — multi-section bills
        total = legacy
    if total is not None:
        fields["total_amount"] = total

    taxable_match = re.search(
        r"(?:Taxable\s*Value|Taxable\s*Amount)\s*[:\-]?\s*[₹$]?\s*([0-9OoIlSB,]+\.?[0-9OoIlSB]*)",
        text,
        re.IGNORECASE,
    )
    if taxable_match:
        fields["taxable_value"] = _num(taxable_match.group(1))

    gst_match = re.search(
        r"(?<![CSI])GST(?!IN)(?:\s*Rate|\s*@)?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*%?",
        text,
        re.IGNORECASE,
    )
    if gst_match:
        rate = float(gst_match.group(1))
        if rate <= 40:  # GST rates are 0-28 in practice; guard against amount mis-match
            fields["gst_rate"] = rate

    # CGST/SGST/IGST — tolerate dropped first letter (OCR "GST: 4500.00")
    for cgst_match in re.finditer(
        r"[CSI]?GST(?!IN)(?:\s*[:\-]\s*|\s+)[₹$]?\s*([0-9OoIlSB,]+\.?[0-9OoIlSB]*)(?![\d.]*\s*%)",
        text,
        re.IGNORECASE,
    ):
        val = _num(cgst_match.group(1))
        if val is None:
            continue
        label = cgst_match.group(0).upper()
        if "CGST" in label:
            fields["cgst"] = val
        elif "SGST" in label:
            fields["sgst"] = val
        elif "IGST" in label:
            fields["igst"] = val
        else:  # bare "GST: X" — split across cgst+sgst if unset
            if fields["cgst"] is None and fields["sgst"] is None:
                fields["cgst"] = fields["sgst"] = round(val / 2, 2)
            elif fields["cgst"] is None:
                fields["cgst"] = val
            elif fields["sgst"] is None:
                fields["sgst"] = val

    hsn_match = re.search(
        r"(?:[Hh]?SN\s*Code|HSN\s*SAC|SAC)\s*[:\-]?\s*(\d{4,8})",
        text,
        re.IGNORECASE,
    )
    if hsn_match:
        fields["hsn_code"] = hsn_match.group(1)

    qty_match = re.search(r"(?:Qty|Quantity)\s*[:\-]?\s*\n?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if not qty_match:
        qty_match = re.search(r"(\d+(?:\.\d+)?)[ \t]*(?:Qty|Quantity)", text, re.IGNORECASE)
    if qty_match:
        fields["quantity"] = float(qty_match.group(1))

    item_match = re.search(
        r"(?:Item|Description|Particulars)\s*[:\-]?\s*(.+?)(?:\n|$)",
        text,
        re.IGNORECASE,
    )
    if item_match:
        candidate = item_match.group(1).strip()
        if candidate and candidate not in ("#", "CODE", "code", "Quantity", "UNIT PRICE", "UNIT PRICE, USD"):
            fields["item_description"] = candidate

    # line items — overrides single desc/qty when rows found
    items = parse_line_items(text)
    if items:
        fields["line_items"] = items
        first_desc = items[0]["desc"]
        if first_desc and first_desc != "—" and len(first_desc) >= 3:
            fields["item_description"] = first_desc
        total_qty = sum(i["qty"] for i in items if i.get("qty"))
        if total_qty:
            fields["quantity"] = round(total_qty, 2)

    found = sum(1 for k, v in fields.items() if k != "confidence" and v is not None and v != 0.0)
    fields["confidence"] = min(found / 10.0, 1.0)
    return fields


def process_invoice_document(file_path: str) -> Dict[str, Any]:
    ext = os.path.splitext(file_path)[1].lower()

    # Fast path: born-digital PDFs have a text layer — extraction is ~ms,
    # OCR is ~90s/page. Try text first, OCR only for scans.
    if ext == ".pdf":
        doc = fitz.open(file_path)
        pages = min(len(doc), MAX_PDF_PAGES)
        text = "\n\n".join(doc[i].get_text("text") for i in range(pages))
        if pages < len(doc):
            text += f"\n\n[OCR stopped after {MAX_PDF_PAGES} of {len(doc)} pages]"
        doc.close()
        if len(text.strip()) > 100:
            fields = parse_invoice_fields(text)
            fields["raw_text"] = text
            fields["source"] = "text"
            items = structured_line_items_from_pdf(file_path)
            if items:
                fields["line_items"] = items
                first_desc = items[0]["desc"]
                if first_desc and first_desc != "—" and len(first_desc) >= 3:
                    fields["item_description"] = first_desc
                total_qty = sum(i["qty"] for i in items if i.get("qty"))
                if total_qty:
                    fields["quantity"] = round(total_qty, 2)
            return fields

    img_paths, warn = document_to_images(file_path)
    parts: list[str] = []
    ocr_items: list[dict] = []
    for p in img_paths:
        try:
            text, items = extract_text_and_items(p)
            parts.append(text)
            ocr_items.extend(items)
        except ValueError:
            parts.append("")  # unreadable page — degrade, don't re-raise
    full_text = "\n\n".join(parts)
    if warn:
        full_text = (full_text + "\n\n" + warn).strip()
    fields = parse_invoice_fields(full_text)
    fields["raw_text"] = full_text
    fields["source"] = "ocr"
    if not ocr_items:
        ocr_items = parse_line_items(full_text)
    if ocr_items:
        fields["line_items"] = ocr_items
        first_desc = ocr_items[0]["desc"]
        if first_desc and first_desc != "—" and len(first_desc) >= 3:
            fields["item_description"] = first_desc
        total_qty = sum(i["qty"] for i in ocr_items if i.get("qty"))
        if total_qty:
            fields["quantity"] = round(total_qty, 2)
    return fields
