import re
from collections import defaultdict

zones = [("desc", 27.0), ("qty", 137.7), ("rate", 157.5), ("rate", 166.4)]
row = [("1", 15.0), ("Concept", 27.0), ("work:", 43.1), ("Requirements", 53.7),
       ("engineering,", 79.7), ("UX", 102.9), ("concept,", 109.5), ("PoC", 125.8),
       ("12", 137.7), ("h", 143.3), ("160.00", 157.5), ("1'920.00", 179.6)]


def zone_of(x0):
    best = None
    for kind, zx in zones:
        if x0 >= zx - 2.0:
            best = kind
        else:
            break
    return best


by = defaultdict(list)
for w, x in row:
    by[zone_of(x)].append((w, x))
for k in sorted(by, key=str):
    print(k, by[k])
