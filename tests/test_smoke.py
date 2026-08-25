import os
import sys
import uuid
from pathlib import Path

# ponytail: ensure tests run from repo root without PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

# ponytail: isolate tests from prod DB (was: drop_all on real ffaa.db)
TEST_DB_PATH = (Path(__file__).parent / "test_ffaa.db").as_posix()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

import pytest
from fastapi.testclient import TestClient

from app.database import engine, SessionLocal
from app.main import app
from app import models
from app.duplicates import score_pair
from app.folders import archive_file
from app.tally import invoices_to_tally_xml
from app.routers.invoices import ALLOWED_EXTENSIONS


def pytest_sessionfinish(session, exitstatus):
    try:
        from app.database import engine
        engine.dispose()
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def clean_db():
    # ponytail: recreate sqlite for each test
    models.Base.metadata.create_all(bind=engine)
    yield
    models.Base.metadata.drop_all(bind=engine)


TEST_PASSWORD = "T3st-Passw0rd!"


def _register_and_login(c: TestClient, email: str | None = None) -> str:
    """Register + login a fresh user; TestClient keeps the ffaaauth cookie."""
    email = email or f"u-{uuid.uuid4().hex}@example.com"
    r = c.post("/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 201, r.text
    r = c.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code in (200, 204), r.text
    return email


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    # slowapi limits are for live abuse; per-test registrations would trip them.
    app.state.limiter.enabled = False
    yield
    app.state.limiter.enabled = True


@pytest.fixture
def anon_client():
    return TestClient(app)


@pytest.fixture
def client():
    c = TestClient(app)
    _register_and_login(c)
    return c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_client(client):
    r = client.post("/api/v1/clients", json={"name": "Test Client"})
    assert r.status_code == 201 or r.status_code == 200
    data = r.json()
    assert data["name"] == "Test Client"
    assert data["id"] is not None


def test_partial_client_update(client):
    # create
    r = client.post("/api/v1/clients", json={"name": "Partial Co", "email": "old@b.com"})
    assert r.status_code in (200, 201)
    cid = r.json()["id"]

    # partial update email only
    r = client.put(f"/api/v1/clients/{cid}", json={"email": "a@b.com"})
    assert r.status_code == 200
    data = r.json()
    assert data["email"] == "a@b.com"
    assert data["name"] == "Partial Co"  # not wiped

    # 404
    r = client.put("/api/v1/clients/99999", json={"email": "x@y.com"})
    assert r.status_code == 404


def test_file_type_gate(client):
    # unsupported extension
    r = client.post(
        "/api/v1/upload-invoice",
        files={"file": ("test.txt", b"not an image", "text/plain")},
        data={"client_id": "1", "invoice_type": "sales"},
    )
    assert r.status_code in (400, 422, 500)
    if r.status_code == 400:
        assert "Unsupported file type" in r.json()["detail"]


def test_allowed_extensions():
    assert ".jpg" in ALLOWED_EXTENSIONS
    assert ".pdf" in ALLOWED_EXTENSIONS
    assert ".png" in ALLOWED_EXTENSIONS
    assert ".tiff" in ALLOWED_EXTENSIONS
    assert ".docx" not in ALLOWED_EXTENSIONS


def test_review_approved_blocked(client):
    # create client
    r = client.post("/api/v1/clients", json={"name": "Review Co"})
    cid = r.json()["id"]
    # create invoice directly in DB
    from app.database import SessionLocal
    from app import models
    from datetime import datetime
    db = SessionLocal()
    try:
        inv = models.Invoice(
            client_id=cid,
            invoice_number="INV-001",
            company_name="Test Co",
            total_amount=1000.0,
            invoice_type="sales",
            status="pending",
            approved=False,
            created_at=datetime.now(),
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        inv_id = inv.id
    finally:
        db.close()
    # approve it
    r = client.put(f"/api/v1/invoices/{inv_id}/approve")
    assert r.status_code == 200
    # try to review — should be 409
    r = client.put(f"/api/v1/invoices/{inv_id}/review", json={
        "client_id": cid,
        "invoice_number": "INV-001",
        "company_name": "Changed",
        "total_amount": 1000,
        "invoice_type": "sales"
    })
    assert r.status_code == 409
    assert r.json()["detail"] == "Invoice already approved"


def test_duplicate_score_exact():
    # ponytail: minimal fake invoice objects
    class FakeInv:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    a = FakeInv(
        invoice_number="INV-001",
        company_name="Acme Corp",
        total_amount=1000.0,
        invoice_date=None,
    )
    b = FakeInv(
        invoice_number="INV-001",
        company_name="Acme Corp",
        total_amount=1000.0,
        invoice_date=None,
    )
    score, fields = score_pair(a, b)
    assert score >= 80.0
    assert "invoice_number" in fields
    assert "company_name" in fields
    assert "total_amount" in fields


def test_folders_archive(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("test")
    archived = archive_file(str(src), "Acme Corp", __import__("datetime").date(2024, 6, 15), "Sales", "source.txt")
    assert "Acme Corp/2024/June/Sales/source.txt" in archived.replace("\\", "/")
    assert os.path.exists(archived)


def test_tally_xml_has_voucher():
    class FakeItem:
        description = "Item A"
        quantity = 1
        rate = 100
        taxable_value = 100
        hsn_code = "1234"
        gst_rate = 18

    class FakeInv:
        invoice_type = "sales"
        invoice_number = "INV-001"
        invoice_date = __import__("datetime").date(2024, 6, 15)
        company_name = "Acme"
        total_amount = 118.0
        taxable_value = 100.0
        cgst = 9.0
        sgst = 9.0
        igst = 0.0
        hsn_code = "1234"
        quantity = 1
        item_description = "Item A"
        items = [FakeItem()]

    xml = invoices_to_tally_xml([FakeInv()])
    assert "<VOUCHER" in xml
    assert "Sales" in xml


def test_invoices_list_filter(client):
    # two clients
    r1 = client.post("/api/v1/clients", json={"name": "Client A"})
    r2 = client.post("/api/v1/clients", json={"name": "Client B"})
    c1, c2 = r1.json()["id"], r2.json()["id"]
    # create invoice for client A (direct DB insert)
    from app.database import SessionLocal
    from app import models
    from datetime import datetime
    db = SessionLocal()
    try:
        inv_a = models.Invoice(
            client_id=c1, invoice_number="INV-A", company_name="A Co",
            total_amount=500.0, invoice_type="sales", status="pending",
            approved=False, created_at=datetime.now(),
        )
        inv_b = models.Invoice(
            client_id=c2, invoice_number="INV-B", company_name="B Co",
            total_amount=700.0, invoice_type="sales", status="pending",
            approved=False, created_at=datetime.now(),
        )
        db.add_all([inv_a, inv_b])
        db.commit()
    finally:
        db.close()
    # list all
    r = client.get("/api/v1/invoices")
    assert len(r.json()) == 2
    # filter by client A
    r = client.get(f"/api/v1/invoices?client_id={c1}")
    assert len(r.json()) == 1
    assert r.json()[0]["invoice_number"] == "INV-A"
    # filter by client B
    r = client.get(f"/api/v1/invoices?client_id={c2}")
    assert len(r.json()) == 1
    assert r.json()[0]["invoice_number"] == "INV-B"


def _upload_invoice(client, cid, filename="inv.jpg"):
    from app.routers import invoices as invoices_router

    def fake_ocr(path):
        return {
            "invoice_number": "INV-2026-001",
            "invoice_date": "15/03/2026",
            "company_name": "Meridian Textiles Pvt Ltd",
            "gst_rate": 18.0,
            "taxable_value": 50000.0,
            "total_amount": 59000.0,
            "cgst": 4500.0,
            "sgst": 4500.0,
            "igst": 0.0,
            "hsn_code": "5208",
            "quantity": 100.0,
            "item_description": "Cotton fabric roll",
            "confidence": 0.9,
        }

    monkey = pytest.MonkeyPatch()
    monkey.setattr(invoices_router, "process_invoice_document", fake_ocr)
    try:
        r = client.post(
            "/api/v1/upload-invoice",
            files={"file": (filename, b"fake image bytes", "image/jpeg")},
            data={"client_id": str(cid), "invoice_type": "sales"},
        )
    finally:
        monkey.undo()
    return r


def test_auto_duplicate_flag_on_upload(client):
    r = client.post("/api/v1/clients", json={"name": "Dupe Co"})
    cid = r.json()["id"]
    r1 = _upload_invoice(client, cid)
    assert r1.status_code == 200
    r2 = _upload_invoice(client, cid, "inv2.jpg")
    assert r2.status_code == 200
    # auto-dupe should have created a pending flag without manual scan
    flags = client.get("/api/v1/duplicates/flags?status=pending").json()
    assert len(flags) == 1
    assert flags[0]["similarity_score"] >= 80.0


def test_invoice_item_created_on_upload(client):
    r = client.post("/api/v1/clients", json={"name": "Item Co"})
    cid = r.json()["id"]
    r = _upload_invoice(client, cid)
    assert r.status_code == 200
    data = r.json()
    assert data["invoice_number"] == "INV-2026-001"
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["description"] == "Cotton fabric roll"
    assert item["taxable_value"] == 50000.0
    assert item["hsn_code"] == "5208"
    assert item["line_total"] == 59000.0


def _make_invoice_row(client, cid, number="INV-DEL", approved=False):
    from app.database import SessionLocal
    from app import models
    from datetime import datetime
    db = SessionLocal()
    try:
        inv = models.Invoice(
            client_id=cid, invoice_number=number, company_name="Del Co",
            total_amount=1000.0, invoice_type="sales", status="pending",
            approved=approved, created_at=datetime.now(),
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        return inv.id
    finally:
        db.close()


def test_delete_invoice_cascade(client):
    r = client.post("/api/v1/clients", json={"name": "Del Client"})
    cid = r.json()["id"]
    inv_id = _make_invoice_row(client, cid)
    # link a bank row + reconciliation
    from app.database import SessionLocal
    from app import models
    from datetime import datetime
    db = SessionLocal()
    try:
        bs = models.BankStatement(client_id=cid, date=datetime.now().date(),
                                  narration="NEFT DEL", debit=1000.0, credit=0.0,
                                  balance=5000.0, reconciled=True, invoice_id=inv_id,
                                  created_at=datetime.now())
        db.add(bs)
        db.commit()
        db.refresh(bs)
        db.add(models.Reconciliation(invoice_id=inv_id, bank_statement_id=bs.id,
                                     match_score=0.9, matched_by="auto", confirmed=True,
                                     created_at=datetime.now()))
        db.commit()
        bank_id = bs.id
    finally:
        db.close()
    r = client.delete(f"/api/v1/invoices/{inv_id}")
    assert r.status_code == 200
    # cascade: invoice gone, reconciliation gone, bank unlinked + open
    assert client.get(f"/api/v1/invoices/{inv_id}").status_code == 404
    banks = client.get(f"/api/v1/bank-statements?client_id={cid}").json()
    assert len(banks) == 1
    assert banks[0]["invoice_id"] is None
    assert banks[0]["reconciled"] is False
    hist = client.get(f"/api/v1/reconciliations?client_id={cid}").json()
    assert hist == []


def test_reconcile_history_and_undo(client):
    r = client.post("/api/v1/clients", json={"name": "Rec Client"})
    cid = r.json()["id"]
    inv_id = _make_invoice_row(client, cid, "INV-REC", approved=True)
    from app.database import SessionLocal
    from app import models
    from datetime import datetime
    db = SessionLocal()
    try:
        bs = models.BankStatement(client_id=cid, date=datetime.now().date(),
                                  narration="NEFT REC", debit=1000.0, credit=0.0,
                                  balance=5000.0, created_at=datetime.now())
        db.add(bs)
        db.commit()
        db.refresh(bs)
        bank_id = bs.id
    finally:
        db.close()
    # confirm reconcile writes history + links
    r = client.post(f"/api/v1/reconcile?client_id={cid}&confirm=true")
    assert r.status_code == 200
    hist = client.get(f"/api/v1/reconciliations?client_id={cid}").json()
    assert len(hist) == 1
    assert hist[0]["invoice_id"] == inv_id
    # bank row now carries the invoice link
    banks = client.get(f"/api/v1/bank-statements?client_id={cid}").json()
    assert banks[0]["invoice_id"] == inv_id
    # undo keeps the bank row but removes the match
    r = client.delete(f"/api/v1/reconciliations/{hist[0]['id']}")
    assert r.status_code == 200
    assert client.get(f"/api/v1/reconciliations?client_id={cid}").json() == []
    banks = client.get(f"/api/v1/bank-statements?client_id={cid}").json()
    assert len(banks) == 1
    assert banks[0]["reconciled"] is False
    assert banks[0]["invoice_id"] is None


def test_bank_upload_preview_and_commit(client):
    r = client.post("/api/v1/clients", json={"name": "Bank Preview Co"})
    cid = r.json()["id"]
    csv = b"Date,Narration,Debit,Credit,Balance\n01/04/2026,Rent,15000,0,35000\n"
    # preview: no insert
    r = client.post(f"/api/v1/bank-statements/upload?client_id={cid}&preview=true",
                    files={"file": ("stmt.csv", csv, "text/csv")})
    assert r.status_code == 200
    assert r.json()["preview"] is True
    assert len(r.json()["rows"]) == 1
    assert client.get(f"/api/v1/bank-statements?client_id={cid}").json() == []
    # commit inserts
    r = client.post(f"/api/v1/bank-statements/upload?client_id={cid}&preview=false",
                    files={"file": ("stmt.csv", csv, "text/csv")})
    assert r.status_code == 200
    rows = client.get(f"/api/v1/bank-statements?client_id={cid}").json()
    assert len(rows) == 1
    assert rows[0]["narration"] == "Rent"


def test_delete_client_cascade(client):
    r = client.post("/api/v1/clients", json={"name": "Client Del"})
    cid = r.json()["id"]
    inv_id = _make_invoice_row(client, cid)
    r = client.delete(f"/api/v1/clients/{cid}")
    assert r.status_code == 200
    clients = client.get("/api/v1/clients").json()
    assert all(c["id"] != cid for c in clients)
    assert client.get(f"/api/v1/invoices/{inv_id}").status_code == 404


def test_gstin_validation():
    from app.audit import validate_gstin
    assert validate_gstin("27AABCM1234A1Z5")  # valid Gujarat PAN-based
    assert not validate_gstin("27AABCM1234A1Z")   # too short
    assert not validate_gstin("XXAABCM1234A1Z5")  # bad state
    assert not validate_gstin("27AABCM1234A1Z5X") # extra char
    assert not validate_gstin("")
    assert not validate_gstin(None)


def test_math_check():
    from app.audit import math_check
    ok, _ = math_check(50000.0, 4500.0, 4500.0, 0.0, 59000.0, 18.0)
    assert ok
    ok2, msg = math_check(50000.0, 100.0, 100.0, 0.0, 59000.0, 18.0)
    assert not ok2
    assert "tax" in msg


def test_audit_in_invoice_response(client):
    r = client.post("/api/v1/clients", json={"name": "Audit Co"})
    cid = r.json()["id"]
    r = _upload_invoice(client, cid)
    assert r.status_code == 200
    data = r.json()
    assert data["audit"] is not None
    assert "math_ok" in data["audit"]
