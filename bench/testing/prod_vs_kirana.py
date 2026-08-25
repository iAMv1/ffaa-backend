import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.ocr import parse_invoice_fields  # noqa: E402

T = json.load(open(os.path.join(os.path.dirname(__file__), "kirana_texts.json")))


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


for name in ("k_015.png", "k_016.png", "k_020.png", "k_023.png", "k_001.png", "k_005.png", "k_009.png"):
    gt = json.load(open(os.path.join(os.path.dirname(__file__), "datasets", "kirana", name.replace(".png", ".gt.json"))))
    sup = gt.get("supplier") or ""
    f = parse_invoice_fields(T[name]["raw_text"])
    got = f.get("company_name")
    ok = bool(sup and got and (norm(got)[:8] in norm(sup) or norm(sup)[:8] in norm(got)))
    print(f"{name}: prod_company={str(got)[:28]!r} gt={sup[:24]!r} ok={ok}")
