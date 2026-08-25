"""TESTING PRIMARY ENGINE — RapidOCR + our parser + our zone line-items.

Mirrors app/ocr.py's interface (process_invoice_document) so bench harnesses
can drop it in. NOT wired into production.

Design (measured): RapidOCR stock (NO binarize — our CLAHE+OTSU HURTS it)
+ parse_invoice_fields + zone-table line items from RapidOCR boxes.
Optional paddle-hybrid fallback for thin detection.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rapidocr_onnxruntime import RapidOCR  # noqa: E402

_ENGINE = None


def _get_engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOCR()
    return _ENGINE


def _cluster_lines(words, tol_factor=0.35):
    import statistics
    words = sorted(words, key=lambda w: (w[2], w[1]))
    ys = [w[2] for w in words]
    gaps = [ys[i + 1] - ys[i] for i in range(len(ys) - 1) if ys[i + 1] > ys[i]]
    median_gap = statistics.median(gaps) if gaps else 20.0
    tol = max(4.0, tol_factor * median_gap)
    lines = []
    for w in words:
        if not lines or w[2] - lines[-1][-1][2] > tol:
            lines.append([w])
        else:
            lines[-1].append(w)
    return lines


def _items_from_word_lines(lines):
    """Zone-table line items (copied from app/ocr.py — shared logic, no import)."""
    import re as _re
    items = []
    header_idx = None
    for li, line in enumerate(lines):
        joined = " ".join(w[0] for w in line).upper()
        if (_re.search(r"DESC|ITEM|PARTICULAR|PRODUCT", joined)
                and _re.search(r"QTY|QUANT|PRICE|RATE|AMOUNT|TOTAL", joined)):
            header_idx = li
            break
    if header_idx is None:
        return items
    zones = []
    for w in lines[header_idx]:
        up = w[0].upper()
        if _re.match(r"QTY|QUANT", up):
            zones.append(("qty", w[1]))
        elif _re.match(r"RATE|UNIT", up) or up.startswith("PRICE"):
            zones.append(("rate", w[1]))
        elif _re.match(r"AMOUNT|TOTAL", up):
            zones.append(("amount", w[1]))
        elif _re.match(r"DESC|ITEM|PARTICULAR", up):
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

    def _num(t):
        t = _re.sub(r"[^0-9,.]", "", t or "").replace(",", "")
        try:
            return float(t)
        except ValueError:
            return None

    def first_num(ws):
        for w in sorted(ws, key=lambda w: w[1]):
            n = _num(w[0])
            if n is not None:
                return n
        return None

    for line in lines[header_idx + 1:]:
        joined = " ".join(w[0] for w in line).upper()
        if _re.search(r"SUBTOTAL|TOTAL|TAX|BALANCE|GRAND", joined):
            break
        by_zone = {}
        for w in line:
            by_zone.setdefault(zone_of(w[1]), []).append(w)
        desc = " ".join(w[0] for w in by_zone.get("desc", []) if _num(w[0]) is None)
        desc = _re.sub(r"^\d+\s+", "", desc).strip()
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
        items.append({"desc": desc or "—", "qty": qty, "rate": rate,
                      "taxable": amount, "hsn": None})
    return items


def extract_text_and_items(image_path: str):
    """One RapidOCR pass → (text, line items). RapidOCR returns boxes."""
    result, _ = _get_engine()(image_path)
    if not result:
        return "", []
    words = [(ln[1], ln[0][0][0], ln[0][0][1]) for ln in result]  # (text, x0, y0)
    words.sort(key=lambda w: (w[2], w[1]))
    text = "\n".join(w[0] for w in words)
    items = _items_from_word_lines(_cluster_lines(words))
    return text, items


def process_invoice_document(file_path: str) -> dict:
    """Same contract as app/ocr.process_invoice_document."""
    from app.ocr import parse_invoice_fields

    if os.path.splitext(file_path)[1].lower() == ".pdf":
        import fitz
        doc = fitz.open(file_path)
        pages = min(len(doc), 20)
        text = "\n\n".join(doc[i].get_text("text") for i in range(pages))
        doc.close()
        if len(text.strip()) > 100:
            fields = parse_invoice_fields(text)
            fields["raw_text"] = text
            fields["source"] = "text"
            return fields

    text, items = extract_text_and_items(file_path)
    fields = parse_invoice_fields(text)
    fields["raw_text"] = text
    fields["source"] = "ocr"
    if items:
        fields["line_items"] = items
    return fields
