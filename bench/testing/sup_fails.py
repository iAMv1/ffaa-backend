import json
import os
import re
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "bench/testing")
from exp_parser2 import exp_supplier  # noqa: E402

T = json.load(open("bench/testing/kirana_texts.json"))


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


shown = 0
for name, d in sorted(T.items()):
    gt = json.load(open(f"bench/testing/datasets/kirana/{name.replace('.png', '.gt.json')}"))
    sup = gt.get("supplier") or ""
    vs = exp_supplier(d["raw_text"])
    ok = bool(sup and vs and (norm(vs)[:8] in norm(sup) or norm(sup)[:8] in norm(vs)))
    if ok or shown >= 4:
        continue
    shown += 1
    print(name, "gt=", repr(sup[:25]), "got=", repr(vs[:25]))
    print("   head:", [l[:40] for l in d["raw_text"].splitlines()[:10]])
