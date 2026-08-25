import sys

sys.path.insert(0, ".")
from app.ocr import _num, parse_line_items

words = ["1", "Concept", "work:", "Requirements", "engineering,", "UX",
         "concept,", "PoC", "12", "h", "160.00", "1'920.00"]
kept = [w for w in words if _num(w) is None]
print("kept by _num:", kept)
print("joined:", " ".join(kept))
txt = ("Pos. Description Quantity Unit price Price\n"
       "1 Concept work: Requirements engineering, UX concept, PoC 12 h 160.00 1'920.00\n"
       "2 Implementation 34 h 160.00 5'440.00\n"
       "Sub-Total 7'360.00")
print("regex items:", parse_line_items(txt))
