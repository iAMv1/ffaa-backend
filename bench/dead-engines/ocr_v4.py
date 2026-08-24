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


def _lines_from_result(r) -> list[tuple[str, float, float]]:
    """(text, top_y, left_x) lines from one PaddleOCR result object."""
    texts = getattr(r, "rec_texts", None)
    if not texts and isinstance(r, dict):
        texts = r.get("rec_texts")
    if not texts:
        return []
    polys = getattr(r, "rec_polys", None)
    if not polys and isinstance(r, dict):
        polys = r.get("rec_polys")
    boxes = getattr(r, "rec_boxes", None)
    if not boxes and isinstance(r, dict):
        boxes = r.get("rec_boxes")
    out = []
    for i, t in enumerate(texts):
        y = x = 0.0
        if i < len(polys) and polys[i] is not None:
            pts = polys[i]
            y = min(p[1] for p in pts)
            x = min(p[0] for p in pts)
        elif i < len(boxes) and boxes[i] is not None:
            b = boxes[i]
            y = min(p[1] for p in b)
            x = min(p[0] for p in b)
        out.append((str(t), y, x))
    return out


def extract_text(image_path: str) -> str:
    engine = _get_ocr_engine()
    result = engine.predict(image_path)
    if not result:
        return ""
    lines = []
    for r in result:
        lines.extend(_lines_from_result(r))
    # reading order: top-to-bottom, then left-to-right (row tolerance ~12px)
    lines.sort(key=lambda t: (round(t[1] / 12.0), t[2]))
    return "\n".join(t for t, _, _ in lines)


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
        r"(?:Invoice\s*(?:No\.?|Number))\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]+)",
        text,
        re.IGNORECASE,
    )
    if inv_match:
        fields["invoice_number"] = inv_match.group(1).strip()

    company_match = re.search(
        r"(?:Company|Vendor|Supplier|Biller)\s*[:\-]?\s*(.+?)(?:\n|$)",
        text,
        re.IGNORECASE,
    )
    if company_match:
        fields["company_name"] = company_match.group(1).strip()

    date_match = re.search(
        r"(?:Date|Dt\.?)\s*[:\-]?\s*(\d{2}[\/\-]\d{2}[\/\-]\d{4}|\d{4}[\/\-]\d{2}[\/\-]\d{2})",
        text,
        re.IGNORECASE,
    )
    if date_match:
        fields["invoice_date"] = date_match.group(1).strip()

    total_match = re.search(
        r"(?:Total\s*(?:Amount)?|Grand\s*Total|Due\s*Amount)\s*[:\-]?\s*[₹$]?\s*([\d,]+\.?\d*)",
        text,
        re.IGNORECASE,
    )
    if total_match:
        fields["total_amount"] = float(total_match.group(1).replace(",", ""))

    taxable_match = re.search(
        r"(?:Taxable\s*Value|Taxable\s*Amount)\s*[:\-]?\s*[₹$]?\s*([\d,]+\.?\d*)",
        text,
        re.IGNORECASE,
    )
    if taxable_match:
        fields["taxable_value"] = float(taxable_match.group(1).replace(",", ""))

    gst_match = re.search(
        r"(?:GST\s*Rate|GST\s*@)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*%",
        text,
        re.IGNORECASE,
    )
    if gst_match:
        fields["gst_rate"] = float(gst_match.group(1))

    cgst_match = re.search(r"CGST\s*[:\-]?\s*[₹$]?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if cgst_match:
        fields["cgst"] = float(cgst_match.group(1).replace(",", ""))

    sgst_match = re.search(r"SGST\s*[:\-]?\s*[₹$]?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if sgst_match:
        fields["sgst"] = float(sgst_match.group(1).replace(",", ""))

    igst_match = re.search(r"IGST\s*[:\-]?\s*[₹$]?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if igst_match:
        fields["igst"] = float(igst_match.group(1).replace(",", ""))

    hsn_match = re.search(
        r"(?:HSN\s*Code|HSN\s*SAC)\s*[:\-]?\s*(\d{4,8})",
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
