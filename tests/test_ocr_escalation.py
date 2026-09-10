"""TDD audit: OCR escalation ladder — judge, merge policy, backends, fail-open.

Decides WHEN a local extraction escalates (math gate + confidence + missing
critical fields — the agentic boundary), HOW results merge (fill empties,
wholesale numerics only when math improves, conflicts keep the original),
and the Datalab cloud backend (submit → poll → download → map). No network
and no weights are ever touched: HTTP is stubbed, Surya-local is asserted to
degrade gracefully when unavailable.
"""
import json
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import os
os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use-0123456789abcdef0123456789abcdef")
os.environ.setdefault("FFAA_DEV", "1")
from app.database import engine
from app.main import app
from app import models
from app.billing import seed_plans
from app.audit import math_check

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


# --- local fixtures ----------------------------------------------------------------

def _healthy_fields():
    return {
        "invoice_number": "INV-100", "company_name": "Acme", "invoice_date": "2025-04-01",
        "gst_rate": 18.0, "taxable_value": 1000.0, "cgst": 90.0, "sgst": 90.0,
        "igst": 0.0, "total_amount": 1180.0, "hsn_code": "9983",
        "rec_score": 0.92, "field_completeness": 0.9, "source": "ocr",
    }


def _broken_fields():
    f = _healthy_fields()
    f.update({"total_amount": 5000.0, "rec_score": 0.4, "field_completeness": 0.5})
    return f


# --- judge: should_escalate ---------------------------------------------------------


def test_judge_escalates_broken_math_with_taxable():
    from app.ocr_escalate import should_escalate

    assert should_escalate(_broken_fields()) is True


def test_judge_spares_unknown_breakdown():
    from app.ocr_escalate import should_escalate

    f = _healthy_fields()
    f.update({"taxable_value": 0.0, "total_amount": 5000.0, "rec_score": 0.9,
              "field_completeness": 0.9})
    assert should_escalate(f) is False


def test_judge_escalates_low_confidence():
    from app.ocr_escalate import should_escalate

    assert should_escalate(_broken_fields()) is True  # rec_score 0.4 < 0.5


def test_judge_spares_healthy_extraction():
    from app.ocr_escalate import should_escalate

    assert should_escalate(_healthy_fields()) is False


def test_judge_escalates_missing_critical_fields():
    from app.ocr_escalate import should_escalate

    f = _healthy_fields()
    f["invoice_number"] = None
    assert should_escalate(f) is True
    g = _healthy_fields()
    g["total_amount"] = 0
    assert should_escalate(g) is True
    h = _healthy_fields()
    h["company_name"] = None
    assert should_escalate(h) is True


def test_judge_escalates_missing_confidence_entirely():
    from app.ocr_escalate import should_escalate

    f = _healthy_fields()
    f.pop("rec_score")
    f.pop("field_completeness")
    assert should_escalate(f) is True


# --- merge policy -------------------------------------------------------------------


def test_merge_fills_empty_fields_from_escalated():
    from app.ocr_escalate import merge

    original = _healthy_fields()
    original["hsn_code"] = None
    original["invoice_number"] = ""
    escalated = {"hsn_code": "5208", "invoice_number": "INV-ESCALATED"}
    merged, notes = merge(original, escalated)
    assert merged["hsn_code"] == "5208"
    assert merged["invoice_number"] == "INV-ESCALATED"
    assert any("hsn_code" in n for n in notes)


def test_merge_keeps_original_on_conflict():
    from app.ocr_escalate import merge

    escalated = {"company_name": "Different Co", "invoice_number": "OTHER"}
    merged, notes = merge(_healthy_fields(), escalated)
    assert merged["company_name"] == "Acme"
    assert merged["invoice_number"] == "INV-100"
    assert any("company_name" in n for n in notes)
    assert any("invoice_number" in n for n in notes)


def test_merge_wholesale_numerics_only_when_math_improves():
    from app.ocr_escalate import merge

    escalated = {
        "taxable_value": 1000.0, "cgst": 90.0, "sgst": 90.0, "igst": 0.0,
        "total_amount": 1180.0, "gst_rate": 18.0,
    }
    merged, notes = merge(_broken_fields(), escalated)
    # original was taxable=1000 total=5000 → broken; escalated is self-consistent
    assert merged["total_amount"] == 1180.0
    assert merged["taxable_value"] == 1000.0
    assert any("escalated math" in n for n in notes)
    ok, _ = math_check(merged["taxable_value"], merged["cgst"], merged["sgst"],
                       merged["igst"], merged["total_amount"], merged["gst_rate"])
    assert ok is True


def test_merge_rejects_escalation_that_is_still_broken():
    from app.ocr_escalate import merge

    original = _broken_fields()
    escalated = {"total_amount": 9999.0, "taxable_value": 10.0}
    merged, notes = merge(original, escalated)
    # escalated math still broken → numerics stay original; conflict noted
    assert merged["total_amount"] == 5000.0
    assert any("conflict" in n for n in notes)


def test_merge_coerces_string_numerics():
    from app.ocr_escalate import merge

    original = _healthy_fields()
    original["total_amount"] = 0
    escalated = {"total_amount": "1180.00", "taxable_value": "1000"}
    merged, notes = merge(original, escalated)
    assert merged["total_amount"] == 1180.0
    assert merged["taxable_value"] == 1000.0


# --- backend factory ----------------------------------------------------------------


def test_factory_off_by_default(monkeypatch):
    from app.ocr_escalate import get_backend

    monkeypatch.delenv("FFAA_OCR_ESCALATION", raising=False)
    assert get_backend() is None


def test_factory_datalab_needs_key(monkeypatch):
    from app.ocr_escalate import get_backend

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.delenv("DATALAB_API_KEY", raising=False)
    assert get_backend() is None
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    backend = get_backend()
    assert backend is not None and backend.name == "datalab-cloud"


def test_factory_unknown_mode_is_off(monkeypatch):
    from app.ocr_escalate import get_backend

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "banana")
    assert get_backend() is None


def test_factory_surya_local_degrades_gracefully(monkeypatch):
    from app.ocr_escalate import get_backend

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "surya")
    backend = get_backend()
    assert backend is not None and backend.name == "surya-local"
    with pytest.raises(Exception):
        backend.extract(__file__)  # unavailable in this deployment — must raise cleanly


# --- Datalab cloud backend (HTTP stubbed) -------------------------------------------


class _FakeHttp:
    """Stub for app.ocr_escalate._http module-level post/get."""

    def __init__(self, responses):
        self.calls = []
        self.responses = list(responses)

    def post(self, url, headers=None, files=None, data=None, timeout=None):
        self.calls.append(("POST", url))
        return self.responses.pop(0)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        return self.responses.pop(0)


def _resp(status, payload):
    class R:
        def __init__(self):
            self.status_code = status
            self._p = payload

        def json(self):
            return self._p

    return R()


COMPLETE = {
    "status": "complete", "success": True,
    "extraction_schema_json": json.dumps({
        "invoice_number": "INV-CLD-1", "company_name": "Cloud Acme",
        "supplier_gstin": "07AAMFA0676L1Z5", "buyer_name": "Urvashi",
        "buyer_gstin": "07BJXPS9205G1ZJ", "place_of_supply": "07",
        "gst_rate": 18.0, "taxable_value": 3850.0, "cgst": 346.5, "sgst": 346.5,
        "igst": 0.0, "total_amount": 4543.0, "hsn_code": "998232",
        "invoice_number_citations": ["b1"],
    }),
}


def _mk_pdf(tmp_path):
    f = tmp_path / "invoice.pdf"
    f.write_bytes(b"%PDF-1.4 escalated-content")
    return str(f)


def _mk_backend(monkeypatch, **kw):
    from app.ocr_escalate import DatalabBackend

    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    return DatalabBackend(max_polls=3, poll_interval=0.0, sleep=lambda s: None, **kw)


def test_datalab_happy_path_maps_fields_and_drops_citations(monkeypatch, tmp_path):
    from app.ocr_escalate import DatalabError

    backend = _mk_backend(monkeypatch)
    http = _FakeHttp([
        _resp(200, {"request_check_url": "https://x/check/1", "status": "pending"}),
        _resp(200, COMPLETE),
    ])
    monkeypatch.setattr("app.ocr_escalate._http", http)
    fields = backend.extract(_mk_pdf(tmp_path))
    assert fields["invoice_number"] == "INV-CLD-1"
    assert fields["supplier_gstin"] == "07AAMFA0676L1Z5"  # schema key mapping
    assert fields["total_amount"] == 4543.0
    assert not any("citations" in k for k in fields)
    assert ("POST", "https://www.datalab.to/api/v1/extract") in http.calls
    assert ("GET", "https://x/check/1") in http.calls


def test_datalab_polls_until_complete(monkeypatch, tmp_path):
    backend = _mk_backend(monkeypatch)
    http = _FakeHttp([
        _resp(200, {"request_check_url": "https://x/check/1", "status": "pending"}),
        _resp(200, {"status": "pending"}),
        _resp(200, COMPLETE),
    ])
    monkeypatch.setattr("app.ocr_escalate._http", http)
    fields = backend.extract(_mk_pdf(tmp_path))
    assert fields["invoice_number"] == "INV-CLD-1"


def test_datalab_failed_status_raises(monkeypatch, tmp_path):
    from app.ocr_escalate import DatalabError

    backend = _mk_backend(monkeypatch)
    http = _FakeHttp([
        _resp(200, {"request_check_url": "https://x/check/1"}),
        _resp(200, {"status": "failed", "error": "bad document"}),
    ])
    monkeypatch.setattr("app.ocr_escalate._http", http)
    with pytest.raises(DatalabError, match="bad document"):
        backend.extract(_mk_pdf(tmp_path))


def test_datalab_submit_error_raises(monkeypatch, tmp_path):
    from app.ocr_escalate import DatalabError

    backend = _mk_backend(monkeypatch)
    http = _FakeHttp([_resp(401, {"detail": "bad key"})])
    monkeypatch.setattr("app.ocr_escalate._http", http)
    with pytest.raises(DatalabError, match="401"):
        backend.extract(_mk_pdf(tmp_path))


def test_datalab_poll_timeout_raises(monkeypatch, tmp_path):
    from app.ocr_escalate import DatalabError

    backend = _mk_backend(monkeypatch)
    http = _FakeHttp([
        _resp(200, {"request_check_url": "https://x/check/1"}),
        _resp(200, {"status": "pending"}),
        _resp(200, {"status": "pending"}),
        _resp(200, {"status": "pending"}),
    ])
    monkeypatch.setattr("app.ocr_escalate._http", http)
    with pytest.raises(DatalabError, match="[Tt]imeout"):
        backend.extract(_mk_pdf(tmp_path))


def test_datalab_result_url_downloaded(monkeypatch, tmp_path):
    backend = _mk_backend(monkeypatch)
    http = _FakeHttp([
        _resp(200, {"request_check_url": "https://x/check/1", "status": "pending"}),
        _resp(200, {"status": "complete", "success": True, "result_url": "https://eu/res/1"}),
        _resp(200, COMPLETE),
    ])
    monkeypatch.setattr("app.ocr_escalate._http", http)
    fields = backend.extract(_mk_pdf(tmp_path))
    assert fields["invoice_number"] == "INV-CLD-1"
    assert ("GET", "https://eu/res/1") in http.calls


def test_datalab_schema_json_as_dict_also_works(monkeypatch, tmp_path):
    backend = _mk_backend(monkeypatch)
    complete_dict = dict(COMPLETE)
    complete_dict["extraction_schema_json"] = json.loads(COMPLETE["extraction_schema_json"])
    http = _FakeHttp([
        _resp(200, {"request_check_url": "https://x/check/1", "status": "pending"}),
        _resp(200, complete_dict),
    ])
    monkeypatch.setattr("app.ocr_escalate._http", http)
    fields = backend.extract(_mk_pdf(tmp_path))
    assert fields["company_name"] == "Cloud Acme"


# --- maybe_escalate: the agentic boundary -------------------------------------------


def test_maybe_escalate_disabled_is_identity(monkeypatch):
    from app.ocr_escalate import maybe_escalate

    monkeypatch.delenv("FFAA_OCR_ESCALATION", raising=False)
    f = _broken_fields()
    assert maybe_escalate("invoice.pdf", f) is f


def test_maybe_escalate_healthy_never_calls_backend(monkeypatch):
    import app.ocr_escalate as esc

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    called = []
    monkeypatch.setattr(esc, "get_backend", lambda: (lambda p: called.append(p) or {})())
    f = _healthy_fields()
    out = esc.maybe_escalate("invoice.pdf", f)
    assert called == []  # judge said no — the agentic loop never fires
    assert out == f


def test_maybe_escalate_broken_merges_and_records_provenance(monkeypatch):
    import app.ocr_escalate as esc

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")

    class FakeBackend:
        name = "datalab-cloud"

        def extract(self, path):
            return {
                "taxable_value": 1000.0, "cgst": 90.0, "sgst": 90.0, "igst": 0.0,
                "total_amount": 1180.0, "gst_rate": 18.0, "hsn_code": "5208",
            }

    monkeypatch.setattr(esc, "get_backend", lambda: FakeBackend())
    out = esc.maybe_escalate("invoice.pdf", _broken_fields())
    assert out["total_amount"] == 1180.0
    # original hsn was non-empty → conflict keeps the original (auditable)
    assert out["hsn_code"] == "9983"
    # provenance must survive persistence via the source column
    assert out["source"] == "ocr+cloud"
    warnings = " ".join(out.get("warnings", []))
    assert "datalab-cloud" in warnings
    assert "conflict hsn_code" in warnings


def test_maybe_escalate_backend_failure_fails_open(monkeypatch):
    import app.ocr_escalate as esc
    from app.ocr_escalate import DatalabError

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")

    class FailingBackend:
        name = "datalab-cloud"

        def extract(self, path):
            raise DatalabError("503 from upstream")

    monkeypatch.setattr(esc, "get_backend", lambda: FailingBackend())
    original = _broken_fields()
    out = esc.maybe_escalate("invoice.pdf", original)
    assert out["total_amount"] == 5000.0  # original kept
    assert any("escalation failed" in w for w in out.get("warnings", []))


def test_datalab_default_mode_is_fast(monkeypatch):
    # cost: escalation is a second opinion on mostly-readable docs;
    # fast ($6/1k) is the cheapest structured tier — balanced was wasteful.
    from app.ocr_escalate import DatalabBackend

    monkeypatch.delenv("FFAA_OCR_DATALAB_MODE", raising=False)
    assert DatalabBackend().extraction_mode == "fast"


def test_budget_exhausted_blocks_escalation(monkeypatch, tmp_path):
    import app.ocr_escalate as esc

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    monkeypatch.setenv("FFAA_OCR_ESCALATION_BUDGET", "2")
    monkeypatch.setenv("FFAA_OCR_ESCALATION_USAGE", str(tmp_path / "usage.json"))
    esc._record_usage(2)  # free tier spent for this month
    out = esc.maybe_escalate("invoice.pdf", _broken_fields())
    assert out["total_amount"] == _broken_fields()["total_amount"]  # local kept
    assert any("budget" in w.lower() for w in out.get("warnings", []))


def test_budget_records_usage_on_success(monkeypatch, tmp_path):
    import app.ocr_escalate as esc

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    monkeypatch.setenv("FFAA_OCR_ESCALATION_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("FFAA_OCR_ESCALATION_BUDGET", "100")
    monkeypatch.setenv("FFAA_OCR_ESCALATION_CACHE", str(tmp_path / "cache.json"))

    class FakeBackend:
        name = "datalab-cloud"

        def extract(self, path):
            return {"taxable_value": 1000.0, "cgst": 90.0, "sgst": 90.0,
                    "igst": 0.0, "total_amount": 1180.0, "gst_rate": 18.0}

    monkeypatch.setattr(esc, "get_backend", lambda: FakeBackend())
    # usage = pages actually BILLED = pages sent (the subset), not the doc total
    monkeypatch.setattr(esc, "_select_send_path", lambda p: ("invoice.pdf", 3, 3))
    out = esc.maybe_escalate("invoice.pdf", _broken_fields())
    assert out["source"] == "ocr+cloud"
    month, pages = esc._read_usage()
    assert pages == 3 and month == esc._current_month()


def test_budget_unset_is_unlimited(monkeypatch, tmp_path):
    import app.ocr_escalate as esc

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    monkeypatch.delenv("FFAA_OCR_ESCALATION_BUDGET", raising=False)
    monkeypatch.setenv("FFAA_OCR_ESCALATION_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("FFAA_OCR_ESCALATION_CACHE", str(tmp_path / "cache.json"))
    esc._record_usage(999)  # usage tracked but no budget -> never blocks

    class FakeBackend:
        name = "datalab-cloud"

        def extract(self, path):
            return {"total_amount": 1180.0, "taxable_value": 1000.0,
                    "cgst": 90.0, "sgst": 90.0, "igst": 0.0, "gst_rate": 18.0}

    monkeypatch.setattr(esc, "get_backend", lambda: FakeBackend())
    out = esc.maybe_escalate("invoice.pdf", _broken_fields())
    assert out["source"] == "ocr+cloud"


def test_cache_hit_skips_backend(monkeypatch, tmp_path):
    # duplicate re-escalation skip: the same document bytes must never pay twice
    import app.ocr_escalate as esc

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    monkeypatch.setenv("FFAA_OCR_ESCALATION_CACHE", str(tmp_path / "cache.json"))
    calls = []

    class FakeBackend:
        name = "datalab-cloud"

        def extract(self, path):
            calls.append(path)
            return {"taxable_value": 1000.0, "cgst": 90.0, "sgst": 90.0,
                    "igst": 0.0, "total_amount": 1180.0, "gst_rate": 18.0}

    monkeypatch.setattr(esc, "get_backend", lambda: FakeBackend())
    pdf = _mk_pdf(tmp_path)
    esc.maybe_escalate(pdf, _broken_fields())
    second = esc.maybe_escalate(pdf, _broken_fields())
    assert len(calls) == 1, "second call must come from cache, not the cloud"
    assert second["total_amount"] == 1180.0
    assert any("cache" in w.lower() for w in second.get("warnings", []))


def test_page_selection_sends_only_weak_pages(monkeypatch, tmp_path):
    # page-selective escalation: bill only pages the local cascade failed at
    import app.ocr_escalate as esc
    import fitz

    doc = fitz.open()
    p1 = doc.new_page()
    for i in range(20):  # single unwrapped lines clip -> page would read weak
        p1.insert_text((72, 72 + i * 14), f"supply of goods and services line {i:02d}")
    doc.new_page()  # near-empty page 2 = suspect (scanned)
    pdf = str(tmp_path / "two.pdf")
    doc.save(pdf)
    doc.close()

    received = []

    class FakeBackend:
        name = "datalab-cloud"

        def extract(self, path):
            with fitz.open(path) as d:
                received.append(d.page_count)
            return {"total_amount": 1180.0, "taxable_value": 1000.0,
                    "cgst": 90.0, "sgst": 90.0, "igst": 0.0, "gst_rate": 18.0}

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    monkeypatch.setattr(esc, "get_backend", lambda: FakeBackend())
    esc.maybe_escalate(pdf, _broken_fields())
    assert received == [1], "only the weak page should be sent/billed"


def test_ladder_ascends_only_when_verification_fails(monkeypatch, tmp_path):
    # the agentic core: next (pricier) tier is attempted ONLY on failed verify
    import app.ocr_escalate as esc

    modes = []

    class FakeBackend:
        name = "datalab-cloud"

        def extract(self, path):
            modes.append(getattr(self, "extraction_mode", "?"))
            if modes[-1] == "fast":
                return {"total_amount": 999.0}  # not gateable -> unverified
            return {"taxable_value": 1000.0, "cgst": 90.0, "sgst": 90.0,
                    "igst": 0.0, "total_amount": 1180.0, "gst_rate": 18.0}

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    monkeypatch.setenv("FFAA_OCR_ESCALATION_TIERS", "fast,balanced")
    monkeypatch.setenv("FFAA_OCR_ESCALATION_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(esc, "get_backend", lambda: FakeBackend())
    out = esc.maybe_escalate(_mk_pdf(tmp_path), _broken_fields())
    assert modes == ["fast", "balanced"]
    assert out["total_amount"] == 1180.0
    assert out["source"] == "ocr+cloud"


def test_ladder_stops_when_verified(monkeypatch, tmp_path):
    import app.ocr_escalate as esc

    modes = []

    class FakeBackend:
        name = "datalab-cloud"

        def extract(self, path):
            modes.append(getattr(self, "extraction_mode", "?"))
            return {"taxable_value": 1000.0, "cgst": 90.0, "sgst": 90.0,
                    "igst": 0.0, "total_amount": 1180.0, "gst_rate": 18.0}

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    monkeypatch.setenv("FFAA_OCR_ESCALATION_TIERS", "fast,balanced")
    monkeypatch.setenv("FFAA_OCR_ESCALATION_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(esc, "get_backend", lambda: FakeBackend())
    esc.maybe_escalate(_mk_pdf(tmp_path), _broken_fields())
    assert modes == ["fast"], "verified on the cheap tier — no pricier call"


def test_default_single_tier_no_ascent(monkeypatch, tmp_path):
    # tiers unset: exactly ONE attempt even when unverified (cost parity)
    import app.ocr_escalate as esc

    modes = []

    class FakeBackend:
        name = "datalab-cloud"

        def extract(self, path):
            modes.append(getattr(self, "extraction_mode", "?"))
            return {"total_amount": 999.0}  # unverifyable

    monkeypatch.setenv("FFAA_OCR_ESCALATION", "datalab")
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    monkeypatch.delenv("FFAA_OCR_ESCALATION_TIERS", raising=False)
    monkeypatch.setenv("FFAA_OCR_ESCALATION_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(esc, "get_backend", lambda: FakeBackend())
    out = esc.maybe_escalate(_mk_pdf(tmp_path), _broken_fields())
    assert modes == ["fast"]
    assert out["total_amount"] != 1180.0  # local fields kept (fail-open)
    assert any("not verified" in w for w in out.get("warnings", []))
