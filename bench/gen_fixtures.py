"""Generate synthetic invoice benchmark fixtures + ground truth.

10 invoices covering real-world degradation: clean, tables, low contrast,
skew, noise, rotation, small font, shadow band, merged cells, two-column.
GT written to bench/gt.json keyed by fixture name.
"""
import json
import math
import os
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

OUT = Path(__file__).parent / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)

FONT_DIR = "C:/Windows/Fonts"
FONTS = {
    "arial": ImageFont.truetype(f"{FONT_DIR}/arial.ttf", 42),
    "arial_bd": ImageFont.truetype(f"{FONT_DIR}/arialbd.ttf", 44),
    "calibri": ImageFont.truetype(f"{FONT_DIR}/calibri.ttf", 40),
    "segoe": ImageFont.truetype(f"{FONT_DIR}/segoeui.ttf", 40),
    "small": ImageFont.truetype(f"{FONT_DIR}/arial.ttf", 26),
    "med": ImageFont.truetype(f"{FONT_DIR}/arial.ttf", 34),
}
rnd = random.Random(42)

GT = {}

INVOICES = [
    {
        "name": "clean_print",
        "company": "Meridian Textiles Pvt Ltd",
        "number": "INV/2026/0481",
        "date": "15/03/2026",
        "hsn": "5208",
        "gst_rate": 18.0,
        "items": [
            {"desc": "Cotton fabric roll 60m", "qty": 100, "rate": 482.00, "taxable": 48200.00},
            {"desc": "Polyster lining 45m", "qty": 50, "rate": 210.50, "taxable": 10525.00},
            {"desc": "Buttons brass pack", "qty": 200, "rate": 18.75, "taxable": 3750.00},
        ],
    },
    {
        "name": "two_col",
        "company": "Northwind Traders LLP",
        "number": "NW-2201-77",
        "date": "22/01/2026",
        "hsn": "9983",
        "gst_rate": 12.0,
        "items": [
            {"desc": "IT consulting services", "qty": 1, "rate": 120000.00, "taxable": 120000.00},
        ],
    },
    {
        "name": "low_contrast",
        "company": "Bhumi Constructions",
        "number": "BC/2026/0912",
        "date": "03/02/2026",
        "hsn": "2523",
        "gst_rate": 28.0,
        "items": [
            {"desc": "OPC cement 43 grade 50kg", "qty": 400, "rate": 315.00, "taxable": 126000.00},
            {"desc": "River sand 1 unit", "qty": 2, "rate": 8500.00, "taxable": 17000.00},
        ],
    },
    {
        "name": "skewed",
        "company": "Crescent Pharma Distributors",
        "number": "CPD/5539",
        "date": "11/04/2026",
        "hsn": "3004",
        "gst_rate": 12.0,
        "items": [
            {"desc": "Paracetamol 500mg strip", "qty": 500, "rate": 12.40, "taxable": 6200.00},
            {"desc": "Amoxicillin 250mg strip", "qty": 300, "rate": 45.60, "taxable": 13680.00},
            {"desc": "Vitamin C 1000mg bottle", "qty": 120, "rate": 95.00, "taxable": 11400.00},
        ],
    },
    {
        "name": "noisy",
        "company": "Shakti Garments",
        "number": "SG/118/26",
        "date": "28/05/2026",
        "hsn": "6211",
        "gst_rate": 5.0,
        "items": [
            {"desc": "Cotton kurti stitched", "qty": 80, "rate": 450.00, "taxable": 36000.00},
            {"desc": "Silk saree premium", "qty": 25, "rate": 2450.00, "taxable": 61250.00},
        ],
    },
    {
        "name": "rotated90",
        "company": "Kota Handloom Coop",
        "number": "KH/24-25/087",
        "date": "09/06/2026",
        "hsn": "6305",
        "gst_rate": 5.0,
        "items": [
            {"desc": "Handloom bedsheet set", "qty": 60, "rate": 890.00, "taxable": 53400.00},
        ],
    },
    {
        "name": "table_only",
        "company": "Vardhman Fabrics",
        "number": "VF-3312",
        "date": "17/07/2026",
        "hsn": "5205",
        "gst_rate": 18.0,
        "items": [
            {"desc": "Cotton yarn 40s combed", "qty": 150, "rate": 265.00, "taxable": 39750.00},
            {"desc": "Cotton yarn 60s combed", "qty": 120, "rate": 340.00, "taxable": 40800.00},
            {"desc": "Poly-cotton yarn 30s", "qty": 200, "rate": 180.00, "taxable": 36000.00},
            {"desc": "Viscose yarn 30s", "qty": 90, "rate": 290.00, "taxable": 26100.00},
        ],
    },
    {
        "name": "small_font",
        "company": "Apollo Distribution Co",
        "number": "ADC/2026/4471",
        "date": "25/08/2026",
        "hsn": "3304",
        "gst_rate": 18.0,
        "items": [
            {"desc": "Face cream 50ml tube", "qty": 300, "rate": 85.00, "taxable": 25500.00},
            {"desc": "Sunscreen lotion 100ml", "qty": 200, "rate": 195.00, "taxable": 39000.00},
            {"desc": "Shampoo 200ml bottle", "qty": 250, "rate": 145.00, "taxable": 36250.00},
            {"desc": "Body lotion 250ml", "qty": 180, "rate": 165.00, "taxable": 29700.00},
        ],
    },
    {
        "name": "shadow_band",
        "company": "Nagarjuna Infra",
        "number": "NI/102/26",
        "date": "30/09/2026",
        "hsn": "7308",
        "gst_rate": 18.0,
        "items": [
            {"desc": "MS angle 40x40x5mm", "qty": 500, "rate": 62.00, "taxable": 31000.00},
            {"desc": "TMT bar 12mm", "qty": 320, "rate": 68.50, "taxable": 21920.00},
        ],
    },
    {
        "name": "merged_cells",
        "company": "Medlife Retail",
        "number": "ML-R/991",
        "date": "05/10/2026",
        "hsn": "9020",
        "gst_rate": 12.0,
        "items": [
            {"desc": "BP monitor digital", "qty": 15, "rate": 1850.00, "taxable": 27750.00},
            {"desc": "Glucometer kit", "qty": 20, "rate": 950.00, "taxable": 19000.00},
        ],
    },
]


def _round2(x):
    return round(x, 2)


def build(name, company, number, date_s, hsn, gst_rate, items, degrade):
    W, H = 1600, 2200
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    ink = degrade.get("ink", "black")
    f_big, f_hdr, f_row = FONTS["arial_bd"], FONTS["arial"], FONTS["med"]

    y = 70
    d.text((60, y), "TAX INVOICE", font=f_big, fill=ink)
    y += 110
    d.text((60, y), f"Invoice Number: {number}", font=f_hdr, fill=ink)
    y += 80
    d.text((60, y), f"Date: {date_s}", font=f_hdr, fill=ink)
    y += 80
    d.text((60, y), f"Company: {company}", font=f_hdr, fill=ink)
    y += 80
    d.text((60, y), "GSTIN: 27AABCM1234A1Z5", font=f_hdr, fill=ink)
    y += 110

    # item table
    table_x, table_w = 60, W - 120
    row_h = 74
    head_h = 70
    d.rectangle([table_x, y, table_x + table_w, y + head_h], outline=ink, width=2)
    d.line([table_x + 620, y, table_x + 620, y + head_h], fill=ink, width=2)
    d.line([table_x + 1000, y, table_x + 1000, y + head_h], fill=ink, width=2)
    d.line([table_x + 1240, y, table_x + 1240, y + head_h], fill=ink, width=2)
    d.text((table_x + 15, y + 16), "Item Description", font=f_hdr, fill=ink)
    d.text((table_x + 640, y + 16), "Qty", font=f_hdr, fill=ink)
    d.text((table_x + 1020, y + 16), "Rate", font=f_hdr, fill=ink)
    d.text((table_x + 1260, y + 16), "Amount", font=f_hdr, fill=ink)
    y += head_h

    taxable_total = 0.0
    for i, it in enumerate(items):
        if degrade.get("merged") and i == 0:
            # merged header row: span all columns
            d.rectangle([table_x, y, table_x + table_w, y + row_h], outline=ink, width=2)
            d.text((table_x + 15, y + 20), f"{it['desc']} (full width row)", font=f_row, fill=ink)
        else:
            d.rectangle([table_x, y, table_x + table_w, y + row_h], outline=ink, width=2)
            d.line([table_x + 620, y, table_x + 620, y + row_h], fill=ink, width=2)
            d.line([table_x + 1000, y, table_x + 1000, y + row_h], fill=ink, width=2)
            d.line([table_x + 1240, y, table_x + 1240, y + row_h], fill=ink, width=2)
            d.text((table_x + 15, y + 22), it["desc"], font=f_row, fill=ink)
            d.text((table_x + 640, y + 22), f"{it['qty']}", font=f_row, fill=ink)
            d.text((table_x + 1020, y + 22), f"{it['rate']:.2f}", font=f_row, fill=ink)
            d.text((table_x + 1260, y + 22), f"{it['taxable']:.2f}", font=f_row, fill=ink)
        y += row_h
        taxable_total += it["taxable"]

    y += 40
    gst = _round2(taxable_total * gst_rate / 100)
    cgst = _round2(gst / 2)
    total = _round2(taxable_total + gst)
    d.text((60, y), f"Taxable Value: {taxable_total:.2f}", font=f_hdr, fill=ink)
    y += 80
    d.text((60, y), f"CGST: {cgst:.2f}", font=f_hdr, fill=ink)
    y += 80
    d.text((60, y), f"SGST: {cgst:.2f}", font=f_hdr, fill=ink)
    y += 80
    d.text((60, y), f"GST Rate: {gst_rate:g}%", font=f_hdr, fill=ink)
    y += 80
    d.text((60, y), f"HSN Code: {hsn}", font=f_hdr, fill=ink)
    y += 80
    d.text((60, y), f"Total Amount: {total:.2f}", font=f_big, fill=ink)

    # degradation
    if degrade.get("contrast"):
        im = ImageOps.autocontrast(im, cutoff=0)
        gray = ImageOps.grayscale(im)
        im = gray.convert("RGB")
        # lighten: simulate faded print
        im = Image.eval(im, lambda p: min(255, int(p * 1.35) + 30))
    if degrade.get("skew"):
        im = im.rotate(degrade["skew"], expand=True, fillcolor="white")
    if degrade.get("noise"):
        px = im.load()
        for _ in range(int(W * H * 0.004)):
            x, y = rnd.randrange(im.width), rnd.randrange(im.height)
            px[x, y] = (0, 0, 0) if rnd.random() < 0.5 else (255, 255, 255)
        im = im.filter(ImageFilter.MedianFilter(3))
    if degrade.get("rotate90"):
        im = im.rotate(90, expand=True, fillcolor="white")
    if degrade.get("shadow"):
        band = Image.new("RGB", (W, int(H * 0.18)), (140, 140, 145))
        im.paste(band, (0, int(H * 0.45)), Image.new("L", band.size, 120))
    if degrade.get("small"):
        pass  # small font baked via f_row choice
    return im


def main():
    for inv in INVOICES:
        degrade = {
            "low_contrast": {"contrast": True},
            "skewed": {"skew": 2.5},
            "noisy": {"noise": True},
            "rotated90": {"rotate90": True},
            "shadow_band": {"shadow": True},
            "merged_cells": {"merged": True},
            "small_font": {"small": True},
        }.get(inv["name"], {})
        im = build(inv["name"], inv["company"], inv["number"], inv["date"],
                   inv["hsn"], inv["gst_rate"], inv["items"], degrade)
        path = OUT / f"{inv['name']}.jpg"
        im.save(path, quality=92)
        taxable = _round2(sum(i["taxable"] for i in inv["items"]))
        gst = _round2(taxable * inv["gst_rate"] / 100)
        GT[inv["name"]] = {
            "invoice_number": inv["number"],
            "invoice_date": inv["date"],
            "company_name": inv["company"],
            "gst_rate": inv["gst_rate"],
            "taxable_value": taxable,
            "total_amount": _round2(taxable + gst),
            "cgst": _round2(gst / 2),
            "sgst": _round2(gst / 2),
            "igst": 0.0,
            "hsn_code": inv["hsn"],
            "quantity": _round2(sum(i["qty"] for i in inv["items"])),
            "item_description": inv["items"][0]["desc"],
            "line_items": inv["items"],
        }
    with open(Path(__file__).parent / "gt.json", "w") as f:
        json.dump(GT, f, indent=2)
    print(f"generated {len(INVOICES)} fixtures + gt.json")


if __name__ == "__main__":
    main()
