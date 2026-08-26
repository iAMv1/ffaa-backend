import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .database import Base, engine
from .billing import seed_plans, catalog_ready
from .users import (
    configured_oauth_providers,
    public_auth_router,
    auth_router,
    limiter,
    current_active_user,
    build_oauth_routers,
    UserRead,
)
from .routers import invoices, clients, bank, tally, duplicates, reminders, billing as billing_router

logger = logging.getLogger("ffaa.billing")

# W3 (M1): create_all never ALTERs existing tables — add the Subscriptions
# cutover columns idempotently; duplicate-column errors mean already applied.
_BILLING_MIGRATION_COLUMNS = [
    ("billing_plans", "rzp_plan_id", "TEXT"),
    ("billing_subscriptions", "rzp_subscription_id", "TEXT"),
    ("billing_subscriptions", "rzp_customer_id", "TEXT"),
    ("billing_subscriptions", "rzp_status", "TEXT"),
    ("billing_subscriptions", "grace_ends_at", "DATETIME"),
    ("billing_subscriptions", "rzp_short_url", "TEXT"),
]


def _run_billing_migration() -> None:
    for table, col, typ in _BILLING_MIGRATION_COLUMNS:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {typ}"))
        except OperationalError:
            pass
    for stmt in (
        "UPDATE billing_subscriptions SET rzp_status='active' "
        "WHERE rzp_status IS NULL AND status='active'",
        "UPDATE billing_subscriptions SET rzp_status='expired' "
        "WHERE rzp_status IS NULL",
    ):
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except OperationalError:
            pass  # column already dropped or never existed (fresh DB)
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE billing_subscriptions DROP COLUMN status"))
    except OperationalError:
        pass  # already dropped (or ancient SQLite without DROP COLUMN)
    # covering indexes for the cap COUNTs (design §4) — idempotent-ish: a
    # duplicate index name raises; ignore.
    for ddl in (
        "CREATE INDEX IF NOT EXISTS ix_clients_owner ON clients (owner_id)",
        "CREATE INDEX IF NOT EXISTS ix_invoices_client_created "
        "ON invoices (client_id, created_at)",
    ):
        with engine.begin() as conn:
            conn.execute(text(ddl))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # W1 (M1): token revocation column (see W1 notes).
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
            ))
    except OperationalError:
        pass
    _run_billing_migration()
    seed_plans()  # idempotent free/pro catalog
    ensure_rzp_plan(get_plan(BillingSession(), "pro") or None) if False else None  # noqa: E501 (lazy; runs on first subscribe)
    app.state.billing_gate_down = not catalog_ready()
    if app.state.billing_gate_down:
        logger.error(
            "billing_plans is EMPTY after seed_plans() — refusing API requests "
            "with 503 billing_unconfigured (audit TENANCY-FAIL-OPEN)"
        )
    yield


app = FastAPI(title="FFAA - Free Accounting Automation", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# slowapi wiring: limiter instance + 429 handler (limits live on the auth routes)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Auth under /api/v1/auth: our rate-limited wrappers (register/login/
# forgot/reset) + the stock router for logout only.
app.include_router(public_auth_router, prefix="/api/v1/auth")
app.include_router(auth_router, prefix="/api/v1/auth")

# Social login: one router per provider whose env keys are configured
# (GOOGLE_OAUTH_CLIENT_ID/SECRET, GITHUB_OAUTH_CLIENT_ID/SECRET).
for _oauth_router, _provider in build_oauth_routers():
    app.include_router(_oauth_router, prefix=f"/api/v1/auth/{_provider}", tags=["auth"])

# /me surface for the FE AuthProvider (plan: fetch /api/v1/me on boot).
# NOTE: fastapi-users' stock get_users_router() is NOT used — its GET /{id}
# route shadows every other single-segment path mounted under /api/v1.
@app.get("/api/v1/auth/providers")
def auth_providers():
    """Advertises configured social-login providers so the FE renders only
    buttons that can actually complete their flow."""
    return {"providers": configured_oauth_providers()}


@app.get("/api/v1/me", response_model=UserRead)
def me(user=Depends(current_active_user)):
    return user


# Boot invariant (design §4): if the plan catalog could not be seeded, refuse
# every API request with 503 billing_unconfigured instead of silently running
# with all caps disabled (audit TENANCY-FAIL-OPEN). /health stays up for probes.
@app.middleware("http")
async def billing_catalog_gate(request: Request, call_next):
    if getattr(app.state, "billing_gate_down", False) and request.url.path.startswith("/api/v1"):
        return JSONResponse(status_code=503, content={"detail": "billing_unconfigured"})
    return await call_next(request)


app.include_router(clients.router, prefix="/api/v1")
app.include_router(invoices.router, prefix="/api/v1")
app.include_router(bank.router, prefix="/api/v1")
app.include_router(tally.router, prefix="/api/v1")
app.include_router(duplicates.router, prefix="/api/v1")
app.include_router(reminders.router, prefix="/api/v1")
app.include_router(billing_router.router, prefix="/api/v1/billing")

@app.get("/health")
def health_check():
    return {"status": "ok"}
