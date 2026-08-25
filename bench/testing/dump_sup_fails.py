"""Dump remaining kirana supplier misses for rule iteration."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exp_parser2 import exp_supplier  # noqa: E402

TEXTS = json.load(open(os.path.join(os.path.dirname(__file__), "kirana_texts.json")))
KIRANA = os.path.join(os.path.dirname(__file__), "datasets", "kirana")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


shown = 0
for name, d in sorted(TEXTS.items()):
    gt = json.load(open(os.path.join(KIRANA, name.replace(".png", ".gt.json"))))
    sup = gt.get("supplier") or ""
    vs = exp_supplier(d["raw_text"])
    ok = bool(sup and vs and (norm(vs)[:8] in norm(sup) or norm(sup)[:8] in norm(vs)))
    if ok or shown >= 8:
        continue
    shown += 1
    print(f"\n== {name} gt={sup!r} got={vs!r}")
    lines = d["raw_text"].splitlines()
    for i, ln in enumerate(lines[:14]):
        print(f"   {i}: {ln[:64]!r}")
