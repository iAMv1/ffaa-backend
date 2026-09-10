"""OCR escalation ladder — the agentic boundary of the extraction pipeline.

Judge (`should_escalate`) decides WHEN the local cascade's result is not
trustworthy enough (broken math on a gated breakdown, low confidence, missing
critical fields). Backends then re-extract: the Datalab cloud (Chandra,
per-field verification — plain commercial API) or a future Surya/Chandra
LOCAL adapter (OpenRAIL-M gated; graceful-unavailable until GPU hardware
exists). `merge` accepts only provable improvements: fills empty fields,
replaces numerics wholesale ONLY when the escalation's own math passes while
the original's failed, and keeps the original on conflict (warnings carry the
provenance). Everything fails open — a dead backend can never lose the local
extraction.
"""
import json
import os
import time
from typing import Any

import httpx

from .audit import math_check

# HTTP facade so tests can stub without patching the httpx module itself.
_http = httpx

# Fields we know how to map into ocr_result / the Invoice row.
FIELD_KEYS = [
    "invoice_number", "invoice_date", "company_name", "supplier_gstin",
    "buyer_name", "buyer_gstin", "place_of_supply", "gst_rate",
    "taxable_value", "cgst", "sgst", "igst", "total_amount", "hsn_code",
]
NUMERIC_KEYS = {"gst_rate", "taxable_value", "cgst", "sgst", "igst", "total_amount"}

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string", "description": "Invoice number or ID as printed"},
        "invoice_date": {"type": "string", "description": "Invoice date (DD/MM/YYYY or YYYY-MM-DD)"},
        "company_name": {"type": "string", "description": "Seller / supplier company name"},
        "supplier_gstin": {"type": "string", "description": "Seller GSTIN (15 chars)"},
        "buyer_name": {"type": "string", "description": "Buyer / bill-to name"},
        "buyer_gstin": {"type": "string", "description": "Buyer GSTIN (15 chars)"},
        "place_of_supply": {"type": "string", "description": "Place of supply state code (2 chars)"},
        "gst_rate": {"type": "number", "description": "GST rate percent (e.g. 18)"},
        "taxable_value": {"type": "number", "description": "Taxable value before tax"},
        "cgst": {"type": "number", "description": "CGST amount"},
        "sgst": {"type": "number", "description": "SGST amount"},
        "igst": {"type": "number", "description": "IGST amount"},
        "total_amount": {"type": "number", "description": "Total invoice amount"},
        "hsn_code": {"type": "string", "description": "HSN/SAC code"},
    },
    "required": ["invoice_number", "total_amount"],
}


class DatalabError(RuntimeError):
    """Cloud backend failure — callers must fail open to the local result."""


class BackendUnavailable(RuntimeError):
    """A local backend cannot run in this deployment (weights/GPU absent)."""


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _math_of(fields: dict) -> bool | None:
    """math_ok for a fields dict; None when the breakdown is not gateable."""
    taxable = _num(fields.get("taxable_value")) or 0.0
    if taxable <= 0:
        return None
    ok, _ = math_check(
        taxable,
        _num(fields.get("cgst")) or 0.0,
        _num(fields.get("sgst")) or 0.0,
        _num(fields.get("igst")) or 0.0,
        _num(fields.get("total_amount")) or 0.0,
        fields.get("gst_rate"),
    )
    return ok


def should_escalate(fields: dict) -> bool:
    """The judge: perceives the local result, decides whether to escalate."""
    thr = float(os.environ.get("FFAA_OCR_ESCALATION_CONF", "0.5"))
    # 1. broken math on a gateable breakdown (same predicate as the approve gate)
    if _math_of(fields) is False:
        return True
    # 2. low or absent confidence
    rec = fields.get("rec_score")
    conf = rec if rec is not None else fields.get("field_completeness")
    if conf is None or float(conf) < thr:
        return True
    # 3. missing critical fields
    if not fields.get("invoice_number") or not fields.get("total_amount") or not fields.get("company_name"):
        return True
    return False


def merge(original: dict, escalated: dict) -> tuple[dict, list[str]]:
    """Accept only provable improvements. Returns (merged_fields, notes)."""
    merged = dict(original)
    notes: list[str] = []

    o_ok = _math_of(original)
    e_ok = _math_of(escalated)

    # Wholesale numerics ONLY when the escalation's own math passes where the
    # original's failed — never on a wash.
    if o_ok is False and e_ok is True:
        for k in NUMERIC_KEYS:
            ev = _num(escalated.get(k))
            if ev is not None:
                merged[k] = ev
        notes.append("escalated math accepted (numerics replaced)")

    for k in FIELD_KEYS:
        if k in NUMERIC_KEYS and (o_ok is False and e_ok is True):
            continue  # already replaced wholesale
        ev = escalated.get(k)
        if ev is None:
            continue
        if k in NUMERIC_KEYS:
            ev = _num(ev)
            if ev is None:
                continue
            ov = _num(merged.get(k))
            if ov is None or ov == 0:
                merged[k] = ev
                notes.append(f"filled {k} from escalation")
            elif abs(ov - ev) > 1e-9:
                notes.append(f"conflict {k}: kept original")
        else:
            ov = merged.get(k)
            if ov is None or ov == "":
                merged[k] = ev
                notes.append(f"filled {k} from escalation")
            elif ov != ev:
                notes.append(f"conflict {k}: kept original")
    return merged, notes


class DatalabBackend:
    """Datalab cloud structured extraction (Chandra). Plain commercial API —
    submit multipart, poll request_check_url, optionally download result_url
    (EU region), map extraction_schema_json into ocr_result keys."""

    name = "datalab-cloud"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        extraction_mode: str | None = None,
        max_polls: int = 30,
        poll_interval: float = 2.0,
        sleep=time.sleep,
    ):
        self.api_key = api_key or os.environ.get("DATALAB_API_KEY", "")
        self.base = (
            base_url or os.environ.get("DATALAB_API_BASE", "https://www.datalab.to/api/v1")
        ).rstrip("/")
        self.extraction_mode = extraction_mode or os.environ.get("FFAA_OCR_DATALAB_MODE", "balanced")
        self.max_polls = max_polls
        self.poll_interval = poll_interval
        self._sleep = sleep
        self.headers = {"X-API-Key": self.api_key}

    def extract(self, file_path: str) -> dict:
        schema = json.dumps(INVOICE_SCHEMA)
        with open(file_path, "rb") as f:
            r = _http.post(
                f"{self.base}/extract",
                headers=self.headers,
                files={"file": (os.path.basename(file_path), f)},
                data={"page_schema": schema, "extraction_mode": self.extraction_mode},
                timeout=120,
            )
        if r.status_code != 200:
            raise DatalabError(f"submit failed {r.status_code}")
        j = r.json()
        check = j.get("request_check_url")
        if not check:
            raise DatalabError("no request_check_url in submit response")

        for _ in range(self.max_polls):
            pr = _http.get(check, headers=self.headers, timeout=60)
            if pr.status_code != 200:
                raise DatalabError(f"poll failed {pr.status_code}")
            pj = pr.json()
            if pj.get("success") is False or pj.get("status") == "failed":
                raise DatalabError(pj.get("error") or "document processing failed")
            if pj.get("status") == "complete":
                payload = pj
                if pj.get("result_url"):
                    dl = _http.get(pj["result_url"], headers=self.headers, timeout=60)
                    if dl.status_code != 200:
                        raise DatalabError(f"result download failed {dl.status_code}")
                    payload = {**dl.json(), **{k: v for k, v in pj.items() if v is not None}}
                raw = payload.get("extraction_schema_json")
                if isinstance(raw, str):
                    raw = json.loads(raw)
                if not isinstance(raw, dict):
                    raise DatalabError("missing extraction payload")
                return {k: raw[k] for k in FIELD_KEYS if raw.get(k) is not None}
            self._sleep(self.poll_interval)
        raise DatalabError("extraction poll timeout")


class SuryaLocalBackend:
    """FUTURE PROSPECT: Surya/Chandra open weights locally need torch + (for
    Chandra) a GPU. This CPU deployment ships the interface only — it fails
    cleanly and the ladder fails open. The OpenRAIL-M posture (sub-threshold
    use, attribution, downstream notice) is documented in LICENSES.md."""

    name = "surya-local"

    def extract(self, file_path: str) -> dict:
        raise BackendUnavailable(
            "surya-local requires torch + model weights (OpenRAIL-M-gated); "
            "not provisioned in this CPU deployment — use FFAA_OCR_ESCALATION=datalab"
        )


def get_backend():
    mode = os.environ.get("FFAA_OCR_ESCALATION", "off").strip().lower()
    if mode in ("", "off", "none"):
        return None
    if mode == "datalab":
        if not os.environ.get("DATALAB_API_KEY"):
            return None
        return DatalabBackend()
    if mode == "surya":
        return SuryaLocalBackend()
    return None


def _with_warning(fields: dict, warning: str) -> dict:
    out = dict(fields)
    out["warnings"] = list(fields.get("warnings") or []) + [warning]
    return out


def maybe_escalate(file_path: str, ocr_result: dict) -> dict:
    """The pipeline hook. Off by default; judge-first (a healthy extraction
    never even constructs a backend); every failure fails open to the local
    result with the reason recorded in warnings."""
    mode = os.environ.get("FFAA_OCR_ESCALATION", "off").strip().lower()
    if mode in ("", "off", "none"):
        return ocr_result
    if not should_escalate(ocr_result):
        return ocr_result
    backend = get_backend()
    if backend is None:
        return _with_warning(ocr_result, "escalation enabled but backend unavailable; keeping local extraction")
    try:
        escalated = backend.extract(file_path)
    except Exception as e:  # fail-open — never lose the local extraction
        return _with_warning(
            ocr_result, f"escalation failed ({type(e).__name__}: {e}); keeping local extraction"
        )
    merged, notes = merge(ocr_result, escalated)
    if notes:
        return _with_warning(merged, f"escalated via {backend.name}: " + "; ".join(notes))
    return merged
