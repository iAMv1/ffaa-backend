import re
import cv2
from typing import Dict, Any
import easyocr

reader = easyocr.Reader(["en"], verbose=False)

def preprocess_image(image_path: str) -> str:
    img = cv2.imread(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray)
    thresh = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    processed_path = (
        image_path.replace(".pdf", "_processed.jpg")
        .replace(".jpg", "_processed.jpg")
        .replace(".png", "_processed.jpg")
    )
    cv2.imwrite(processed_path, thresh)
    return processed_path

def extract_text(image_path: str) -> str:
    result = reader.readtext(image_path, detail=0)
    return "\n".join(result)

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
    processed_path = preprocess_image(file_path)
    text = extract_text(processed_path)
    fields = parse_invoice_fields(text)
    fields["raw_text"] = text
    return fields
