from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .database import Base, engine
from .billing import seed_plans
from .users import public_auth_router, auth_router, limiter, current_active_user, UserRead
from .routers import invoices, clients, bank, tally, duplicates, reminders, billing as billing_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # W1 (M1): create_all won't ALTER an existing users table — add the
    # revocation column idempotently; duplicate-column means it's already there.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
            ))
    except OperationalError:
        pass
    seed_plans()  # idempotent free/pro catalog (P4)
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

# /me surface for the FE AuthProvider (plan: fetch /api/v1/me on boot).
# NOTE: fastapi-users' stock get_users_router() is NOT used — its GET /{id}
# route shadows every other single-segment path mounted under /api/v1.
@app.get("/api/v1/me", response_model=UserRead)
def me(user=Depends(current_active_user)):
    return user
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
