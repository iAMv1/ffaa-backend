import re

lines = open("bench/testing/verify_final.log", encoding="utf-8", errors="replace").read().splitlines()
tot_ok = tot_bad = 0
for ln in lines:
    m = re.match(r"(k_\d+\.png): total=([\d.]+)/([\d.]+) comp=", ln)
    if not m:
        continue
    pred, gt = float(m.group(2)), float(m.group(3))
    if abs(pred - gt) / gt <= 0.01:
        tot_ok += 1
    else:
        tot_bad += 1
        print("MISS:", m.group(1), "pred", pred, "gt", gt)
print(f"kirana totals: {tot_ok} exact, {tot_bad} miss (of {tot_ok + tot_bad} shown)")
