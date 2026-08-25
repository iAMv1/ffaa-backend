"""Dump remaining kirana total failures for rule iteration."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exp_parser2 import exp_total  # noqa: E402

TEXTS = json.load(open(os.path.join(os.path.dirname(__file__), "kirana_texts.json")))
KIRANA = os.path.join(os.path.dirname(__file__), "datasets", "kirana")

shown = 0
for name, d in sorted(TEXTS.items()):
    gt = json.load(open(os.path.join(KIRANA, name.replace(".png", ".gt.json"))))
    gt_total = gt.get("grand_total")
    nt = exp_total(d["raw_text"])
    if nt is not None and abs(nt - gt_total) / gt_total <= 0.01:
        continue
    if shown >= 5:
        break
    shown += 1
    print(f"\n== {name} pred={nt} GT={gt_total}")
    lines = d["raw_text"].splitlines()
    for i, ln in enumerate(lines):
        if re.search(r"GRAND|SUBTOTAL|TOTAL|AMOUNT", ln.upper()) and re.search(r"\d", ln):
            print(f"   {i}: {ln[:70]!r}")
