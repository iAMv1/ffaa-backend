"""Unhappy-path audit: uploads must fail gracefully (4xx), never 500, and the
S1 invariants hold (no file_path leakage in error responses; staging never
survives a failure).

Deliberately never feeds real OCR: every case is stopped at the extension
gate, the size gate, or the parse gate before any model load.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import os
os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use-0123456789abcdef0123456789abcdef")
os.environ.setdefault("FFAA_DEV", "1")
from app.database import engine
from app.main import app
from app import models
from app.billing import seed_plans

TEST_PASSWORD = "T3st-Passw0rd!"


@pytest.fixture(autouse=True)
def clean_db():
    models.Base.metadata.drop_all(bind=engine)
    models.Base.metadata.create_all(bind=engine)
    seed_plans()  # fail-closed billing gate needs the catalog (design §4)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    app.state.limiter.enabled = False
    yield
    app.state.limiter.enabled = True


def _register_and_login(c: TestClient) -> str:
    email = f"u-{uuid.uuid4().hex}@example.com"
    r = c.post("/api/v1/auth/register", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 201, r.text
    r = c.post("/api/v1/auth/login", data={"username": email, "password": TEST_PASSWORD})
    assert r.status_code in (200, 204), r.text
    return email


@pytest.fixture
def client():
    c = TestClient(app)
    _register_and_login(c)
    cid = c.post("/api/v1/clients", json={"name": "Upload Co"}).json()["id"]
    return c, cid


# --- invoice upload: extension gate -------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["payload.exe", "report", "a.pdf.exe", "nul.txt.bak"],
)
def test_invoice_upload_rejects_bad_extensions(client, filename):
    c, cid = client
    r = c.post(
        "/api/v1/upload-invoice",
        files={"file": (filename, b"x")},
        data={"client_id": str(cid)},
    )
    assert r.status_code == 400, f"{filename}: {r.text[:200]}"
    assert "Unsupported file type" in r.json()["detail"]


def test_invoice_upload_oversized_is_413_and_leaks_no_path(client, monkeypatch):
    c, cid = client
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "1")
    r = c.post(
        "/api/v1/upload-invoice",
        files={"file": ("big.jpg", b"x" * (2 * 1024 * 1024))},
        data={"client_id": str(cid)},
    )
    assert r.status_code == 413, r.text
    assert "MAX_FILE_SIZE_MB" in r.json()["detail"]
    body = r.json()
    assert "file_path" not in str(body)  # S1: internal paths never leak


def test_invoice_upload_missing_file_field_is_422(client):
    c, cid = client
    r = c.post("/api/v1/upload-invoice", data={"client_id": str(cid)})
    assert r.status_code == 422, r.text


# --- batch upload: whole-batch scoping + per-file graceful failures -----------------


def test_batch_upload_foreign_client_fails_whole_batch(client):
    c, _ = client
    other = TestClient(app)
    _register_and_login(other)
    other_cid = other.post("/api/v1/clients", json={"name": "Other Co"}).json()["id"]
    r = c.post(
        "/api/v1/upload-invoices",
        files={"files": ("a.jpg", b"x")},
        data={"client_id": str(other_cid)},
    )
    assert r.status_code == 404, r.text


def test_batch_upload_per_file_graceful_failure(client):
    c, cid = client
    r = c.post(
        "/api/v1/upload-invoices",
        files=[
            ("files", ("bad.exe", b"x")),
            ("files", ("bad2", b"x")),
        ],
        data={"client_id": str(cid)},
    )
    assert r.status_code == 200, r.text
    results = r.json()
    assert len(results) == 2
    assert all(res["status"] == "failed" for res in results)
    assert all(res["invoice"] is None for res in results)
    # error text names the failure class, not internal paths
    assert all("file_path" not in (res["error"] or "") for res in results)


# --- bank upload: parse gate + size gate + scoping ----------------------------------
# client_id is a QUERY param on the bank upload route (mirrors the FE call).


def test_bank_upload_garbage_csv_is_400_graceful(client):
    c, cid = client
    r = c.post(
        f"/api/v1/bank-statements/upload?client_id={cid}&preview=true",
        files={"file": ("stmt.csv", b"not,a,real,statement!!!\ngarbage,line,here")},
    )
    assert r.status_code == 400, r.text
    assert "No rows parsed" in r.json()["detail"]


def test_bank_upload_oversized_is_413(client, monkeypatch):
    c, cid = client
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "1")
    r = c.post(
        f"/api/v1/bank-statements/upload?client_id={cid}",
        files={"file": ("big.csv", b"x" * (2 * 1024 * 1024))},
    )
    assert r.status_code == 413, r.text


def test_bank_upload_missing_file_is_422(client):
    c, cid = client
    r = c.post(f"/api/v1/bank-statements/upload?client_id={cid}")
    assert r.status_code == 422, r.text


def test_bank_upload_missing_client_id_is_422(client):
    c, _ = client
    r = c.post("/api/v1/bank-statements/upload", files={"file": ("s.csv", b"a,b\n1,2")})
    assert r.status_code == 422, r.text


def test_bank_upload_foreign_client_is_404(client):
    c, _ = client
    other = TestClient(app)
    _register_and_login(other)
    other_cid = other.post("/api/v1/clients", json={"name": "Other Bank Co"}).json()["id"]
    r = c.post(
        f"/api/v1/bank-statements/upload?client_id={other_cid}&preview=true",
        files={"file": ("stmt.csv", b"date,narration,debit,credit,balance\n2025-01-01,Rent,100,0,900")},
    )
    assert r.status_code == 404, r.text


def test_bank_upload_unknown_client_id_is_404(client):
    c, _ = client
    r = c.post(
        "/api/v1/bank-statements/upload?client_id=999999&preview=true",
        files={"file": ("stmt.csv", b"date,narration\n2025-01-01,Rent")},
    )
    assert r.status_code == 404, r.text
