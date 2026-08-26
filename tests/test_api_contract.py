"""API contract drift guard.

Pins every path ffaa-frontend/src/api.ts calls to app.openapi() — no network.
Rename/move an endpoint or response model the FE depends on and the build
fails; update BOTH api.ts and FE_CALLS together. Audit trail:
.scratch/ffaa-saas/design/api-contract.md.

Guard extensions (W1, api-contract.md "## Guard extensions (W1)"):
  1. Response-schema assertions check openapi() response $ref names
     (route.response_model), not str(content) membership — the substring
     probe passed even if the $ref vanished.
  2. Login requestBody media type pinned: application/x-www-form-urlencoded.
  3. Success status codes pinned: register 201, forgot-password 202,
     login 204 (live-verified), logout 204.
  4. Nested 402 fixture {"detail": {"detail", "upgrade", "plan"}} pinned.
  5. Nested-detail 503 payments_not_configured fixture pinned.
"""
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("FFAA_SECRET", "test-secret-do-not-use")
os.environ.setdefault("FFAA_DEV", "1")

from app.main import app  # noqa: E402
from app.routers import bank, clients, duplicates, invoices, reminders, tally  # noqa: E402

# Every call ffaa-frontend/src/api.ts makes, verbatim: (method, path).
# NOTE: session status is /me, NOT /auth/me — both halves pinned below.
FE_CALLS = [
    ("GET", "/health"), ("GET", "/api/v1/me"),
    ("POST", "/api/v1/auth/register"), ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"), ("POST", "/api/v1/auth/forgot-password"),
    ("POST", "/api/v1/auth/reset-password"), ("PATCH", "/api/v1/auth/account"),
    ("GET", "/api/v1/clients"), ("POST", "/api/v1/clients"),
    ("PUT", "/api/v1/clients/{client_id}"), ("DELETE", "/api/v1/clients/{client_id}"),
    ("GET", "/api/v1/clients/{client_id}/folders"),
    ("POST", "/api/v1/clients/{client_id}/send-reminder"),
    ("GET", "/api/v1/invoices"), ("POST", "/api/v1/upload-invoice"),
    ("POST", "/api/v1/upload-invoices"), ("PUT", "/api/v1/invoices/{invoice_id}/approve"),
    ("PUT", "/api/v1/invoices/{invoice_id}/review"), ("DELETE", "/api/v1/invoices/{invoice_id}"),
    ("POST", "/api/v1/invoices/{invoice_id}/duplicates/check"),
    ("GET", "/api/v1/bank-statements"), ("POST", "/api/v1/bank-statements/upload"),
    ("DELETE", "/api/v1/bank-statements/{bank_id}"), ("POST", "/api/v1/reconcile"),
    ("GET", "/api/v1/reconciliations"), ("DELETE", "/api/v1/reconciliations/{recon_id}"),
    ("GET", "/api/v1/duplicates/flags"), ("POST", "/api/v1/duplicates/flags/{flag_id}/resolve"),
    ("GET", "/api/v1/reminders/history"), ("GET", "/api/v1/reminders/preview"),
    ("DELETE", "/api/v1/reminders/{reminder_id}"), ("GET", "/api/v1/export-tally"),
]

SPEC = app.openapi()
LIVE = {
    (m.upper(), path): ops[m]
    for path, ops in SPEC["paths"].items()
    for m in ops
    if m in ("get", "post", "put", "patch", "delete")
}

# Endpoints that MUST stay public (no cookie auth) — landing/pricing + health.
PUBLIC_PATHS = {"/health", "/api/v1/billing/plans"}
# fastapi-users routes that are public BY DESIGN (pre-auth flows).
AUTH_PUBLIC = {
    "/api/v1/auth/register", "/api/v1/auth/login",
    "/api/v1/auth/forgot-password", "/api/v1/auth/reset-password",
}


def _router_endpoints(*routers):
    """{(method, path): handler} across plain APIRouters (unprefixed paths)."""
    out = {}
    for r in routers:
        for route in r.routes:
            for method in getattr(route, "methods", ()):
                if method in ("HEAD", "OPTIONS"):
                    continue
                out[(method, route.path)] = route.endpoint
    return out

ENDPOINTS = _router_endpoints(
    clients.router, invoices.router, bank.router, tally.router,
    duplicates.router, reminders.router,
)


def test_every_fe_call_exists_with_exact_method():
    missing = [f"{m} {p}" for m, p in FE_CALLS if (m, p) not in LIVE]
    assert not missing, f"FE calls routes the backend no longer serves: {missing}"


def test_no_auth_me_route():
    """Session status lives at /me. A future /auth/me would fork the contract."""
    forks = [p for _, p in LIVE if p.endswith("/auth/me")]
    assert not forks, f"found {forks} — session status is GET /api/v1/me; remove the fork"


def test_fe_relied_routes_require_auth():
    """Every tenant route the FE calls must be cookie-authenticated."""
    unguarded = []
    for method, path in FE_CALLS:
        if LIVE[(method, path)].get("security"):
            continue
        if path in PUBLIC_PATHS or path in AUTH_PUBLIC:
            continue
        unguarded.append(f"{method} {path}")
    assert not unguarded, f"tenant endpoints lost their auth dependency: {unguarded}"


def test_public_endpoints_stay_public():
    for path in sorted(PUBLIC_PATHS):
        ops = SPEC["paths"].get(path)
        assert ops, f"public endpoint vanished: {path}"
        for m, op in ops.items():
            if m in ("get", "post", "put", "patch", "delete"):
                assert not op.get("security"), f"{path} must stay unauthenticated"


def _ref_name(schema: dict) -> str | None:
    """Schema name a route's response_model produced ($ref, or array item)."""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return _ref_name(schema["items"])
    return None


def test_key_response_schemas_pinned():
    """Response models other code (and generated FE types) are built on.

    W1: asserted against the actual $ref (route.response_model), not substring
    membership in str(content) — the old probe passed even if the $ref vanished.
    """
    expected = {
        ("GET", "/api/v1/clients"): ["ClientOut"],
        ("PUT", "/api/v1/clients/{client_id}"): ["ClientOut"],
        ("GET", "/api/v1/invoices"): ["InvoiceOut"],
        ("POST", "/api/v1/upload-invoice"): ["InvoiceOut"],
        ("POST", "/api/v1/upload-invoices"): ["UploadFileResult"],
        ("POST", "/api/v1/clients/{client_id}/send-reminder"): ["EmailReminderOut"],
        ("GET", "/api/v1/reminders/history"): ["EmailReminderOut"],
        ("GET", "/api/v1/bank-statements"): ["BankStatementOut"],
        ("POST", "/api/v1/invoices/{invoice_id}/duplicates/check"): ["DuplicateScanResult"],
        ("POST", "/api/v1/duplicates/flags/{flag_id}/resolve"): ["DuplicateFlagOut"],
        ("GET", "/api/v1/reconciliations"): ["ReconciliationOut"],
        ("GET", "/api/v1/me"): ["UserRead"],
        ("PATCH", "/api/v1/auth/account"): ["UserRead"],
    }
    for (method, path), names in expected.items():
        content = LIVE[(method, path)]["responses"]["200"].get("content", {})
        schema = content.get("application/json", {}).get("schema")
        assert schema is not None, f"{method} {path} lost its JSON response schema"
        got = _ref_name(schema)
        assert got in names, (
            f"{method} {path} response $ref is {got!r}, expected one of {names}; "
            "regenerate FE types (npm run gen:api) and re-audit api.ts"
        )


def test_success_status_codes_pinned():
    """W1 #3: FE's auth flow branches on these exact codes."""
    expected = {
        ("POST", "/api/v1/auth/register"): "201",
        ("POST", "/api/v1/auth/login"): "204",
        ("POST", "/api/v1/auth/logout"): "204",
        ("POST", "/api/v1/auth/forgot-password"): "202",
    }
    drifted = {
        f"{m} {p}: got {sorted(LIVE[(m, p)]['responses'])}, want {code}"
        for (m, p), code in expected.items()
        if code not in LIVE[(m, p)]["responses"]
    }
    assert not drifted, f"auth success status codes drifted: {drifted}"


def test_login_requestbody_is_form_encoded():
    """W1 #2: OAuth2 form shape the stock login and the FE both assume."""
    body = LIVE[("POST", "/api/v1/auth/login")].get("requestBody", {}).get("content", {})
    assert "application/x-www-form-urlencoded" in body, sorted(body)
    assert "application/json" not in body, "login must stay form-encoded, not JSON"


def test_402_payload_fixture_pinned():
    """W1 #4: over-cap rejections nest the upgrade CTA — FE checkout will need it.
    Fixture shape per api-contract.md; pinned against the raising site."""
    from app.tenancy import require_entitlement
    src = inspect.getsource(require_entitlement).replace("'", '"')
    for fragment in ('"detail": f"Your', '"upgrade": True', '"plan": code'):
        assert fragment in src, (
            f"402 payload fixture drifted: missing {fragment!r}; "
            "update api-contract.md §Entitlement gate + this guard together"
        )


def test_503_payments_not_configured_fixture_pinned():
    """W1 #5: absent Razorpay keys → 503 with nested detail {"detail": str}."""
    from app.routers import billing as billing_router
    src = inspect.getsource(billing_router).replace("'", '"')
    assert 'detail={"detail": "payments not configured"}' in src, (
        '503 body must stay nested: {"detail": {"detail": "payments not configured"}}'
    )


def test_delete_shape_is_deleted_id():
    """All five delete endpoints answer {"deleted": <id>} — FE types depend on it."""
    for method, path in [
        ("DELETE", "/clients/{client_id}"),
        ("DELETE", "/invoices/{invoice_id}"),
        ("DELETE", "/bank-statements/{bank_id}"),
        ("DELETE", "/reminders/{reminder_id}"),
        ("DELETE", "/reconciliations/{recon_id}"),
    ]:
        src = inspect.getsource(ENDPOINTS[(method, path)])
        assert '"deleted"' in src.replace("'", '"'), (
            f"{method} {path} response shape drifted away from {{'deleted': id}}"
        )
