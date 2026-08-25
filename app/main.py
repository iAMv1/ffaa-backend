from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .database import Base, engine
from .routers import invoices, clients, bank, tally, duplicates, reminders
from .users import auth_router, register_router, reset_router, limiter, current_active_user, UserRead


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
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

# Auth: register/login/logout/forgot/reset under /api/v1/auth (plan: P1).
app.include_router(auth_router, prefix="/api/v1/auth")
app.include_router(register_router, prefix="/api/v1/auth")
app.include_router(reset_router, prefix="/api/v1/auth")

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

@app.get("/health")
def health_check():
    return {"status": "ok"}
