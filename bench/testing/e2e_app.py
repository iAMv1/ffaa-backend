"""E2E: full application flow against REAL documents (fresh DB).

Flow: client → bank PDF uploads (4 real + 1 image) → invoice PDF → invoice
image (kirana) → review → approve → reconcile → dupe scan → reminder
preview/send → Tally export → folders + file download.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import engine  # noqa: E402
from app import models  # noqa: E402
from app.main import app  # noqa: E402

models.Base.metadata.drop_all(bind=engine)
models.Base.metadata.create_all(bind=engine)
c = TestClient(app)
BANK = os.path.join(os.path.dirname(__file__), "datasets", "bank_pdfs")
REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "real")
KIRANA = os.path.join(os.path.dirname(__file__), "datasets", "kirana", "k_000.png")

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {detail}", flush=True)
    else:
        failed += 1
        print(f"  FAIL {name} {detail}", flush=True)


# 1. client
r = c.post("/api/v1/clients", json={"name": "E2E Test Co"})
check("create client", r.status_code in (200, 201), f"-> {r.status_code}")
cid = r.json()["id"]

# 2. bank PDFs + image (PNG is out of spec — CSV/TXT/PDF only → expect 400)
for f, expect in (("rbc_sample.pdf", 200), ("leapfin_stripe.pdf", None),
                  ("ukvi_example.pdf", 200), ("novus_meridian.pdf", 200),
                  ("commons_chequing.png", 400)):
    p = os.path.join(BANK, f)
    t0 = time.time()
    r = c.post(f"/api/v1/bank-statements/upload?client_id={cid}",
               files={"file": (f, open(p, "rb"), "application/pdf")})
    dt = round(time.time() - t0, 1)
    if r.status_code == 400 and expect == 400:
        check(f"bank {f} (by-design 400)", True, f"-> 400 {dt}s")
        continue
    if r.status_code == 200:
        n = r.json()["imported"]
        rows = r.json()["rows"]
        has_amt = any(x["debit"] or x["credit"] for x in rows[:5])
        check(f"bank {f}", n > 0 and has_amt, f"-> {n} rows, amt={has_amt}, {dt}s")
    else:
        check(f"bank {f}", False, f"-> {r.status_code} {r.text[:80]}")

banks = c.get(f"/api/v1/bank-statements?client_id={cid}").json()
check("bank rows persisted", len(banks) > 0, f"-> {len(banks)} rows")

# 3. invoice PDF (azure, born-digital)
t0 = time.time()
r = c.post("/api/v1/upload-invoice",
           files={"file": ("invoice.pdf", open(os.path.join(REAL, "invoice.pdf"), "rb"), "application/pdf")},
           data={"client_id": str(cid), "invoice_type": "sales"})
dt = round(time.time() - t0, 1)
inv1 = r.json() if r.status_code == 200 else {}
check("upload azure pdf", r.status_code == 200 and inv1.get("total_amount") == 110.0,
      f"-> total={inv1.get('total_amount')} audit={inv1.get('audit')} {dt}s")
check("audit present", inv1.get("audit") is not None, f"-> {inv1.get('audit')}")

# 4. invoice image (kirana real Indian)
t0 = time.time()
r = c.post("/api/v1/upload-invoice",
           files={"file": ("k000.png", open(KIRANA, "rb"), "image/png")},
           data={"client_id": str(cid), "invoice_type": "purchase"})
dt = round(time.time() - t0, 1)
inv2 = r.json() if r.status_code == 200 else {}
check("upload kirana image", r.status_code == 200 and abs((inv2.get("total_amount") or 0) - 45226.44) <= 1,
      f"-> total={inv2.get('total_amount')} company={str(inv2.get('company_name'))[:20]} {dt}s")

# 5. review + approve inv1
r = c.put(f"/api/v1/invoices/{inv1['id']}/review", json={
    "client_id": cid, "invoice_number": "INV-E2E-1", "company_name": "E2E Co",
    "total_amount": 110.0, "invoice_type": "sales"})
check("review invoice", r.status_code == 200 and r.json()["status"] == "reviewed", f"-> {r.status_code}")
r = c.put(f"/api/v1/invoices/{inv1['id']}/approve")
check("approve invoice", r.status_code == 200 and r.json()["approved"], f"-> {r.status_code}")
r = c.put(f"/api/v1/invoices/{inv1['id']}/review", json={
    "client_id": cid, "invoice_number": "X", "company_name": "X",
    "total_amount": 1, "invoice_type": "sales"})
check("approved blocks review", r.status_code == 409, f"-> {r.status_code}")

# 6. reconcile (endpoint works; amounts unlikely to match real docs)
r = c.post(f"/api/v1/reconcile?client_id={cid}&confirm=true")
check("reconcile confirm", r.status_code == 200, f"-> {r.status_code} matches={len(r.json()['matches'])}")
r = c.get(f"/api/v1/reconciliations?client_id={cid}")
check("reconcile history", r.status_code == 200, f"-> {len(r.json())} pairs")

# 7. dupe scan
r = c.post(f"/api/v1/invoices/{inv2['id']}/duplicates/check")
check("dupe scan", r.status_code == 200, f"-> flags={r.json()['flags_created']}")

# 8. reminders
r = c.get("/api/v1/reminders/preview?days=30")
check("reminder preview", r.status_code == 200, f"-> {len(r.json())} clients")
r = c.post(f"/api/v1/clients/{cid}/send-reminder", json={"send": True})
check("reminder send (SMTP expected fail)", r.status_code == 200 and r.json()["status"] in ("failed", "sent"),
      f"-> {r.json().get('status')} {r.json().get('error_message', '')[:40]}")
r = c.get(f"/api/v1/reminders/history?client_id={cid}")
check("reminder history", len(r.json()) >= 1, f"-> {len(r.json())} rows")

# 9. tally export
r = c.get(f"/api/v1/export-tally?client_id={cid}")
check("tally export", r.status_code == 200 and b"<VOUCHER" in r.content,
      f"-> {r.status_code} {len(r.content)} bytes")

# 10. folders + file download
r = c.get(f"/api/v1/clients/{cid}/folders")
check("folders listing", r.status_code == 200 and r.json()["folders"] != {},
      f"-> {list(r.json()['folders'].keys())}")
tree = r.json()["folders"]
first = None
for y, months in tree.items():
    for m, cats in months.items():
        for cat, files in cats.items():
            if files:
                first = f"{y}/{m}/{cat}/{files[0]}"
                break
        if first:
            break
    if first:
        break
if first:
    r = c.get(f"/api/v1/clients/{cid}/files/{first}")
    check("file download", r.status_code == 200 and len(r.content) > 100, f"-> {r.status_code} {len(r.content)}b")

print(f"\nE2E: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
