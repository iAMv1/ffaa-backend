"""Build a YONO-style word-broken column PDF and verify line reconstruction."""
import os
import sys

import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.path.join(os.path.dirname(__file__), "yono_test.pdf")
doc = fitz.open()
page = doc.new_page(width=595, height=842)
rows = [
    ("27/12/2024", "UPI/GOOGLE PAY/MERCHANT STORE", "1,234.56", "9,765.44"),
    ("28/12/2024", "DEBIT CARD PURCHASE - SWIGGY", "456.00", "9,309.44"),
    ("29/12/2024", "PAYMENT RECEIVED - THANKS", "5,000.00 Cr", "14,309.44"),
]
y = 100
for date, narr, amt, bal in rows:
    for text, x in ((date, 40), (narr, 180), (amt, 480), (bal, 560)):
        page.insert_text((x, y), text, fontsize=10)
    y += 30
doc.save(OUT)
doc.close()

# 1) naive text extraction — word-broken streams
d2 = fitz.open(OUT)
naive = d2[0].get_text("text")
d2.close()
print("--- naive get_text ---")
print(naive[:300])

# 2) clustered reconstruction
from app.bank_parse import _pdf_words_clustered_text, parse_bank_pdf
clustered = _pdf_words_clustered_text(OUT)
print("--- clustered ---")
print(clustered[:300])
rows_out = parse_bank_pdf(OUT)
print("--- parsed rows:", len(rows_out))
for r in rows_out[:2]:
    print("   ", r["date"], r["debit"], r["credit"], r["balance"], repr(r["narration"][:30]))
