"""PaddleOCR CPU + PyMuPDF PDF→image. Shared extract_text for invoices + bank."""
import os
import re
import tempfile
from typing import Any, Dict

import cv2
import fitz

os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_use_mkldnn", "0")

_OCR = None
MAX_PDF_PAGES = 20
PDF_DPI = 200


def _get_ocr_engine():
    global _OCR
    if _OCR is None:
        from paddleocr import PaddleOCR

        _OCR = PaddleOCR(lang="en")
    return _OCR


def extract_text(image_path: str) -> str:
    engine = _get_ocr_engine()
    result = engine.predict(image_path)
    if not result:
        return ""
    texts = []
    for r in result:
        rec_texts = getattr(r, "rec_texts", None)
        if rec_texts:
            texts.extend(rec_texts)
        elif isinstance(r, dict) and r.get("rec_texts"):
            texts.extend(r["rec_texts"])
    return "\n".join(texts)


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
        pix.save(img_path)
        img_paths.append(img_path)
    doc.close()
    return img_paths, warn


def preprocess_image(image_path: str) -> str:
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Could not read image/PDF: {image_path}")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray)
    thresh = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    processed_path = (
        image_path.replace(".pdf", "_processed.jpg")
        .replace(".jpg", "_processed.jpg")
        .replace(".jpeg", "_processed.jpg")
        .replace(".png", "_processed.jpg")
        .replace(".bmp", "_processed.jpg")
        .replace(".tif", "_processed.jpg")
        .replace(".tiff", "_processed.jpg")
    )
    if processed_path == image_path:
        processed_path = image_path + "_processed.jpg"
    cv2.imwrite(processed_path, thresh)
    return processed_path


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
    """Parse money string from OCR text: strip ₹/INR/commas, fix digit confusions."""
    t = re.sub(r"[^0-9OoIlSB,.\-]", "", text)
    t = _fix_digits(t)
    t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _num_field(label_re: str, text: str) -> float | None:
    m = re.search(label_re, text, re.IGNORECASE)
    if not m:
        return None
    return _num(m.group(1))


def parse_line_items(text: str) -> list[dict]:
    """Extract table rows: <desc> <qty> <rate> <amount>. Returns list of dicts."""
    items = []
    in_table = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if "item description" in low or "particulars" in low:
            in_table = True
            continue
        if "taxable value" in low or "grand total" in low or "total amount" in low:
            in_table = False
            continue
        if not in_table:
            continue
        # desc ... qty rate amount | desc ... amount | numeric-only line
        m = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+([0-9,]+\.?\d*)\s*$", line)
        if not m:
            m = re.match(r"^(.*?)\s+([0-9,]+\.?\d*)\s*$", line)
        if m:
            desc = m.group(1).strip()
            nums = [float(x.replace(",", "")) for x in m.groups()[1:] if re.fullmatch(r"[0-9,]+\.?\d*", x)]
            if desc and nums:
                qty = rate = None
                taxable = nums[-1]
                if len(nums) >= 3:
                    qty, rate = nums[-3], nums[-2]
                elif len(nums) == 2:
                    qty = nums[-2]
                items.append({
                    "desc": desc,
                    "qty": qty,
                    "rate": rate,
                    "taxable": taxable,
                    "hsn": None,
                })
    return items


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

    inv_match = re.search(
        r"(?:Invoice\s*(?:No\.?|Number))\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/ .]+)",
        text,
        re.IGNORECASE,
    )
    if inv_match:
        fields["invoice_number"] = re.sub(r"\s+", "", inv_match.group(1).strip())

    company_match = re.search(
        r"(?:Company|Vendor|Supplier|Biller|Party)\s*(?:Name)?\s*[:\-]?\s*([A-Za-z0-9&.,' ]+?)(?:\n|$)",
        text,
        re.IGNORECASE,
    )
    if company_match:
        fields["company_name"] = company_match.group(1).strip().rstrip(":")

    date_match = re.search(
        r"(?:Date|Dt\.?|Invoice\s*Date)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2})",
        text,
        re.IGNORECASE,
    )
    if date_match:
        fields["invoice_date"] = date_match.group(1).strip()

    total_match = re.search(
        r"(?:Total\s*(?:Amount)?|Grand\s*Total|Due\s*Amount)\s*[:\-]?\s*[₹$]?\s*([0-9OoIlSB,]+\.?[0-9OoIlSB]*)",
        text,
        re.IGNORECASE,
    )
    if total_match:
        fields["total_amount"] = _num(total_match.group(1))

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
        r"[CSI]?GST(?!IN)(?:\s*[:\-]\s*|\s+)[₹$]?\s*([0-9OoIlSB,]+\.?[0-9OoIlSB]*)",
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

    qty_match = re.search(r"(?:Qty|Quantity)\s*[:\-]?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if not qty_match:
        qty_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:Qty|Quantity)", text, re.IGNORECASE)
    if qty_match:
        fields["quantity"] = float(qty_match.group(1))

    item_match = re.search(
        r"(?:Item|Description|Particulars)\s*[:\-]?\s*(.+?)(?:\n|$)",
        text,
        re.IGNORECASE,
    )
    if item_match:
        fields["item_description"] = item_match.group(1).strip()

    # line items (R4: table reconstruction) — overrides single desc/qty when rows found
    items = parse_line_items(text)
    if items:
        fields["line_items"] = items
        fields["item_description"] = items[0]["desc"]
        total_qty = sum(i["qty"] for i in items if i.get("qty"))
        if total_qty:
            fields["quantity"] = round(total_qty, 2)

    found = sum(1 for k, v in fields.items() if k != "confidence" and v is not None and v != 0.0)
    fields["confidence"] = min(found / 10.0, 1.0)
    return fields


def process_invoice_document(file_path: str) -> Dict[str, Any]:
    img_paths, warn = document_to_images(file_path)
    parts: list[str] = []
    for p in img_paths:
        ext = os.path.splitext(p)[1].lower()
        if ext == ".pdf":
            parts.append(extract_text(p))
        else:
            try:
                processed = preprocess_image(p)
                parts.append(extract_text(processed))
            except ValueError:
                parts.append(extract_text(p))
    full_text = "\n\n".join(parts)
    if warn:
        full_text = (full_text + "\n\n" + warn).strip()
    fields = parse_invoice_fields(full_text)
    fields["raw_text"] = full_text
    return fields
