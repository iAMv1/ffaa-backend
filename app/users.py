"""fastapi-users wiring: User schemas, UserManager, JWT-cookie auth backend.

Mounted under /api/v1/auth (register / login / logout / forgot-password /
reset-password). `/me` is served from app.routers-style dependency
`current_active_user`, exported here for every tenant router.

Rate limits (slowapi): register 5/h/IP, login 10/h/IP, forgot-password 3/h/IP
(plan revision #5 — forgot-password otherwise doubles as an SMTP bombing vector).
"""
import os
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from fastapi_users import FastAPIUsers, BaseUserManager, IntegerIDMixin
from fastapi_users import schemas as fu_schemas
from fastapi_users.exceptions import InvalidPasswordException, UserAlreadyExists
from fastapi_users.authentication import (
    AuthenticationBackend,
    CookieTransport,
    JWTStrategy,
)
from fastapi_users.db import BaseUserDatabase
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import SessionLocal
from pydantic import BaseModel, EmailStr
from .models import User

# Dev default keeps local dev frictionless; .env.example documents FFAA_SECRET
# and production MUST set it explicitly.
FFAA_SECRET = os.environ.get("FFAA_SECRET", "dev-insecure-secret-change-me")
COOKIE_SECURE = os.environ.get("FFAA_COOKIE_SECURE", "false").strip().lower() == "true"

limiter = Limiter(key_func=get_remote_address)


class SyncUserDatabase(BaseUserDatabase[User, int]):
    """fastapi-users adapter over the project's sync SessionLocal.

    Upstream fastapi-users-db-sqlalchemy is async-engine-only; this keeps one
    sync engine/stack for the whole app (SQLite, same as every other router).
    """

    def __init__(self, session: Session, user_table: type[User] = User):
        self.session = session
        self.user_table = user_table

    async def get(self, id: int):
        return self.session.get(self.user_table, id)

    async def get_by_email(self, email: str):
        return (
            self.session.query(self.user_table)
            .filter(func.lower(self.user_table.email) == email.lower())
            .first()
        )

    async def create(self, create_dict: dict):
        user = self.user_table(**create_dict)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    async def update(self, user: User, update_dict: dict):
        for field, value in update_dict.items():
            setattr(user, field, value)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    async def delete(self, user: User):
        self.session.delete(user)
        self.session.commit()

    # OAuth accounts are out of scope (plan: no social login).
    async def get_by_oauth_account(self, oauth_account_name: str, account_id: str):
        raise NotImplementedError("OAuth not supported")

    async def add_oauth_account(self, user: User, create_dict: dict):
        raise NotImplementedError("OAuth not supported")

    async def update_oauth_account(self, user: User, oauth_account, update_dict: dict):
        raise NotImplementedError("OAuth not supported")


def get_user_db():
    db = SessionLocal()
    try:
        yield SyncUserDatabase(db)
    finally:
        db.close()


class UserManager(IntegerIDMixin, BaseUserManager[User, int]):
    reset_password_token_secret = FFAA_SECRET
    verification_token_secret = FFAA_SECRET


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


# --- Schemas ------------------------------------------------------------------


class UserRead(fu_schemas.BaseUser[int]):
    created_at: datetime | None = None


class UserCreate(fu_schemas.BaseUserCreate):
    pass


class UserUpdate(fu_schemas.BaseUserUpdate):
    pass



# --- Transport / strategy / backend ---------------------------------------------

# httpOnly cookie transport — no token storage in JS (plan: Auth flow).
cookie_transport = CookieTransport(
    cookie_name="ffaaauth",
    cookie_max_age=3600,
    cookie_secure=COOKIE_SECURE,
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=FFAA_SECRET, lifetime_seconds=3600)


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, int](get_user_manager, [auth_backend])
current_active_user = fastapi_users.current_user(active=True)


# --- Routers + abuse limits ------------------------------------------------------

register_router = fastapi_users.get_register_router(UserRead, UserCreate)
reset_router = fastapi_users.get_reset_password_router()
auth_router = fastapi_users.get_auth_router(auth_backend)


def _apply_limit(router, path: str, limit: str) -> None:
    """slowapi decorators need an endpoint carrying a `Request` param — all
    fastapi-users routes have one, so re-wrap after router construction."""
    for route in router.routes:
        if getattr(route, "path", None) == path:
            wrapped = limiter.limit(limit)(route.endpoint)
            # Patch both: newer FastAPI rebuilds serving dependants from
            # route.endpoint, older ones call route.dependant.call directly.
            route.endpoint = wrapped
            route.dependant.call = wrapped


class AccountUpdate(BaseModel):
    email: EmailStr | None = None
    new_password: str | None = None
    current_password: str | None = None


@auth_router.patch("/account", response_model=UserRead)
async def update_account(
    request: Request,
    payload: AccountUpdate,
    user: User = Depends(current_active_user),
    user_manager: UserManager = Depends(get_user_manager),
):
    """Authenticated account self-service (PATCH /api/v1/auth/account).

    Email and password changes both require current_password — an
    account-takeover guard, since a hijacked session alone must not be able
    to lock the owner out. JWT strategy note: existing tokens stay valid
    until expiry after a password change; revocation would need a server-side
    session store.
    """
    if not payload.email and not payload.new_password:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if not payload.current_password:
        raise HTTPException(status_code=400, detail="Current password is required")

    ok, upgraded_hash = user_manager.password_helper.verify_and_update(
        payload.current_password, user.hashed_password
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Incorrect current password")
    if upgraded_hash is not None:  # legacy hash scheme got re-hashed
        await user_manager.user_db.update(user, {"hashed_password": upgraded_hash})

    updates: dict = {}
    if payload.new_password:
        updates["password"] = payload.new_password
    if payload.email and payload.email.lower() != (user.email or "").lower():
        updates["email"] = payload.email

    try:
        # _update hashes `password` (validating strength) and rejects taken emails.
        await user_manager.update(UserUpdate(**updates), user)
    except InvalidPasswordException as e:
        raise HTTPException(status_code=400, detail=f"Invalid password: {e.reason}")
    except UserAlreadyExists:
        raise HTTPException(status_code=400, detail="Email already registered")
    return user


_apply_limit(register_router, "/register", "5/hour")
_apply_limit(auth_router, "/login", "10/hour")
_apply_limit(reset_router, "/forgot-password", "3/hour")
_apply_limit(auth_router, "/account", "10/hour")
