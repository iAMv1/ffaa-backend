"""Generate 42 unique text-layer invoice PDFs (distinct companies/numbers/amounts)."""
import os
import random

import fitz

OUT = os.path.join(os.path.dirname(__file__), "unique42")
os.makedirs(OUT, exist_ok=True)

rnd = random.Random(7)
COMPANIES = ["Meridian Textiles", "Northwind Traders", "Bhumi Constructions", "Crescent Pharma",
             "Shakti Garments", "Kota Handloom", "Vardhman Fabrics", "Apollo Distribution",
             "Nagarjuna Infra", "Medlife Retail", "Lakshmi Steels", "Surya Electronics",
             "Ganga Papers", "Himalaya Foods", "Ocean Logistics", "Prime Chemicals",
             "Royal Furnishings", "Sunrise Agro", "Titan Engineering", "Unity Plastics",
             "Vertex Software", "Western Cables", "Xenon Motors", "Yash Textiles",
             "Zenith Optics", "Alpha Traders", "Beta Imports", "Gamma Exports",
             "Delta Constructions", "Epsilon Media", "Zeta Beverages", "Eta Garments",
             "Theta Jewelers", "Iota Logistics", "Kappa Hardware", "Lambda Pharma",
             "Mu Software", "Nu Automobiles", "Xi Fabrics", "Omicron Foods", "Pi Electronics", "Rho Papers"]

def make(path, i, comp, n):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    lines = [
        "TAX INVOICE",
        f"Invoice Number: {n}",
        f"Date: {rnd.randint(1, 28):02d}/{rnd.randint(1, 12):02d}/2026",
        f"Company: {comp}",
        "GSTIN: 27AABCM1234A1Z5",
        "Item Description        Qty     Rate      Amount",
        "Cotton fabric 60m        100     482.00    48200.00",
        "Polyster lining 45m      50      210.50    10525.00",
        "Taxable Value: 58725.00",
        "CGST: 5285.25",
        "SGST: 5285.25",
        "GST Rate: 18%",
        "HSN Code: 5208",
        "Total Amount: 69295.50",
    ]
    page.insert_text((50, 50), "\n".join(lines), fontsize=11)
    doc.save(path)
    doc.close()

for i, (comp, n) in enumerate(zip(COMPANIES, [f"INV-2026-{1000+i}" for i in range(len(COMPANIES))])):
    make(os.path.join(OUT, f"u{i:02d}.pdf"), i, comp, n)
print("generated", len(COMPANIES), "unique text-layer PDFs")
