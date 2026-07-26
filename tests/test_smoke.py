import os
import sys
from pathlib import Path

# ponytail: ensure tests run from repo root without PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.database import engine, SessionLocal
from app.main import app
from app import models
from app.duplicates import score_pair
from app.folders import archive_file
from app.tally import invoices_to_tally_xml


@pytest.fixture(autouse=True)
def clean_db():
    # ponytail: recreate sqlite for each test
    models.Base.metadata.create_all(bind=engine)
    yield
    models.Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


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
