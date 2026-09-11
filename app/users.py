"""fastapi-users wiring: User schemas, UserManager, JWT-cookie auth backend.

Mounted under /api/v1/auth. Login/register/forgot-password/reset-password are
thin wrappers we own, decorated with slowapi limits directly (the old
_apply_limit monkeypatch on third-party route objects is gone). Stock auth
router serves logout only. `/me` is served via the `current_active_user`
dependency exported here for every tenant router.

Sessions are versioned JWTs (cookie ffaaauth): claims carry tv=token_version;
any password/email change bumps the column and every outstanding cookie dies
on next request (401).
"""
import logging
import os
from datetime import datetime

import jwt as pyjwt  # PyJWTError sentinel only; token codecs come from fastapi_users.jwt
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import FastAPIUsers, BaseUserManager, IntegerIDMixin, exceptions as fu_exc
from fastapi_users import schemas as fu_schemas
from fastapi_users.exceptions import InvalidPasswordException, UserAlreadyExists
from fastapi_users.authentication import (
    AuthenticationBackend,
    CookieTransport,
    JWTStrategy,
)
from fastapi_users.db import BaseUserDatabase
from fastapi_users.jwt import decode_jwt, generate_jwt
from fastapi_users.router.common import ErrorCode, ErrorModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import SessionLocal
from pydantic import BaseModel, EmailStr
from .email_service import send_email
from .models import OAuthAccount, User

# FFAA_SECRET is REQUIRED outside explicit dev mode (FFAA_DEV=1): a known
# fallback key would let anyone forge auth cookies (audit finding AUTH-SECRET-FALLBACK).
_DEV_MARKER = os.environ.get("FFAA_DEV", "").strip().lower() == "1"
FFAA_SECRET = os.environ.get("FFAA_SECRET", "")
if not FFAA_SECRET:
    if _DEV_MARKER:
        FFAA_SECRET = "dev-insecure-secret-change-me"
    else:
        raise RuntimeError(
            "FFAA_SECRET is not set. Generate one (e.g. python -c \"import secrets; print(secrets.token_urlsafe(48))\") "
            "and put it in .env — or set FFAA_DEV=1 for throwaway local runs."
        )
# Reset/verification tokens use DERIVED secrets, never the JWT key itself.
import hashlib as _hashlib

RESET_TOKEN_SECRET = _hashlib.sha256(b"reset:" + FFAA_SECRET.encode()).hexdigest()
VERIFICATION_TOKEN_SECRET = _hashlib.sha256(b"verify:" + FFAA_SECRET.encode()).hexdigest()
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

    # --- OAuth (Google/GitHub social login) ---

    async def get_by_oauth_account(self, oauth_account_name: str, account_id: str):
        return (
            self.session.query(self.user_table)
            .join(models.OAuthAccount)
            .filter(
                models.OAuthAccount.oauth_name == oauth_account_name,
                models.OAuthAccount.account_id == account_id,
            )
            .first()
        )

    async def add_oauth_account(self, user: User, create_dict: dict):
        account = models.OAuthAccount(**create_dict)
        user.oauth_accounts.append(account)
        self.session.add(user)
        self.session.commit()
        return user

    async def update_oauth_account(self, user: User, oauth_account, update_dict: dict):
        for field, value in update_dict.items():
            setattr(oauth_account, field, value)
        self.session.add(user)
        self.session.commit()
        return user

def get_user_db():
    db = SessionLocal()
    try:
        yield SyncUserDatabase(db)
    finally:
        db.close()


class UserManager(IntegerIDMixin, BaseUserManager[User, int]):
    reset_password_token_secret = RESET_TOKEN_SECRET
    verification_token_secret = VERIFICATION_TOKEN_SECRET

    async def forgot_password(self, user: User, request: Request | None = None) -> None:
        """Stock flow with one addition: the reset JWT embeds tv=token_version,
        so a link minted before any later rotation is refused even though its
        signature and password fingerprint still verify. fastapi-users v15
        inlines token creation here, so this public method IS the seam."""
        if not user.is_active:
            raise fu_exc.UserInactive()
        token_data = {
            "sub": str(user.id),
            "password_fgpt": self.password_helper.hash(user.hashed_password),
            "aud": self.reset_password_token_audience,
            "tv": int(user.token_version or 0),
        }
        token = generate_jwt(
            token_data,
            self.reset_password_token_secret,
            self.reset_password_token_lifetime_seconds,
        )
        await self.on_after_forgot_password(user, token, request)

    async def reset_password(
        self, token: str, password: str, request: Request | None = None
    ) -> User:
        # Pre-check the tv claim, then hand off to the untouched stock logic.
        try:
            data = decode_jwt(
                token,
                self.reset_password_token_secret,
                [self.reset_password_token_audience],
            )
            user = await self.get(self.parse_id(data["sub"]))
        except (
            pyjwt.PyJWTError,
            KeyError,
            fu_exc.UserNotExists,
            fu_exc.InvalidID,
        ):
            raise fu_exc.InvalidResetPasswordToken()
        if int(data.get("tv", 0)) != int(user.token_version or 0):
            raise fu_exc.InvalidResetPasswordToken()
        return await super().reset_password(token, password, request)

    async def on_after_forgot_password(self, user: User, token: str, request: Request | None = None) -> None:
        """Send the reset link. The endpoint keeps its 202 contract even when
        SMTP is unconfigured/failed (no user enumeration, no 500) — the real
        failure is logged loudly for the operator instead."""
        base = os.environ.get("FFAA_PUBLIC_URL", "http://localhost:5173")
        link = f"{base.rstrip('/')}/reset-password?token={token}"
        try:
            send_email(
                user.email,
                "Reset your ParchAI password",
                "We received a request to reset your ParchAI password.\n\n"
                f"Reset it here (link valid for 1 hour):\n{link}\n\n"
                "If you didn't request this, ignore this email — your password stays unchanged.\n",
            )
        except Exception as e:
            logging.getLogger(__name__).error(
                "password reset email FAILED for user %s: %s", user.id, e
            )


    async def on_after_reset_password(self, user: User, request: Request | None = None) -> None:
        # Rotation bumps the version → every session cookie dies (design D1).
        await self.user_db.update(user, {"token_version": int(user.token_version or 0) + 1})

    async def on_after_login(self, user: User, request: Request, response) -> None:
        # Social-login callbacks land on an API route with a blank body; the
        # ffaaauth cookie is already on this response — send the browser home.
        if "/callback" in request.url.path:
            response.status_code = 302
            public = os.environ.get("FFAA_PUBLIC_URL", "http://localhost:5173")
            response.headers["Location"] = f"{public}/app"


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


class VersionedJWTStrategy(JWTStrategy):
    """Session JWTs embed tv=users.token_version (design D1).

    read_token returns None on version mismatch → the authenticator answers
    401. A missing claim means a pre-W1 token; grace: treated as version 0
    (decided — no fleet-wide logout).
    """

    async def write_token(self, user: User) -> str:
        data = {
            "sub": str(user.id),
            "aud": self.token_audience,
            "tv": int(getattr(user, "token_version", 0) or 0),
        }
        return generate_jwt(
            data, self.encode_key, self.lifetime_seconds, algorithm=self.algorithm
        )

    async def read_token(
        self, token: str | None, user_manager: BaseUserManager[User, int]
    ) -> User | None:
        user = await super().read_token(token, user_manager)
        if user is None:
            return None
        try:
            payload = decode_jwt(
                token,
                self.decode_key,
                self.token_audience,
                algorithms=[self.algorithm],
            )
        except pyjwt.PyJWTError:
            return None
        if int(payload.get("tv", 0)) != int(getattr(user, "token_version", 0) or 0):
            return None  # cookie predates a rotation → revoked everywhere
        return user


def get_jwt_strategy() -> VersionedJWTStrategy:
    return VersionedJWTStrategy(secret=FFAA_SECRET, lifetime_seconds=3600)


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, int](get_user_manager, [auth_backend])
current_active_user = fastapi_users.current_user(active=True)


# --- Routers + abuse limits ------------------------------------------------------

# Public auth surface: thin wrappers WE own, so slowapi decorators bind
# natively (design D5). The _apply_limit monkeypatch on third-party route
# objects is deleted. Stock auth router below serves logout only — its login
# route is dropped at mount prep because our wrapper replaces it.
public_auth_router = APIRouter()
auth_router = fastapi_users.get_auth_router(auth_backend)
auth_router.routes = [
    r for r in auth_router.routes if getattr(r, "path", None) != "/login"
]


@public_auth_router.post(
    "/login",
    name="auth:jwt.login",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorModel},
    },
)
@limiter.limit("10/hour")
async def login(
    request: Request,
    credentials: OAuth2PasswordRequestForm = Depends(),
    user_manager: UserManager = Depends(get_user_manager),
    strategy=Depends(auth_backend.get_strategy),
):
    """Form-encoded OAuth2 shape (contract pin). Sets the ffaaauth cookie."""
    user = await user_manager.authenticate(credentials)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorCode.LOGIN_BAD_CREDENTIALS,
        )
    # backend.login returns the Response with the cookie already set.
    return await auth_backend.login(strategy, user)

@public_auth_router.post(
    "/register",
    name="register:register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_400_BAD_REQUEST: {"model": ErrorModel}},
)
@limiter.limit("5/hour")
async def register(
    request: Request,
    user_create: UserCreate,
    user_manager: UserManager = Depends(get_user_manager),
):
    try:
        created_user = await user_manager.create(user_create, safe=True, request=request)
    except UserAlreadyExists:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorCode.REGISTER_USER_ALREADY_EXISTS,
        )
    except InvalidPasswordException as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": ErrorCode.REGISTER_INVALID_PASSWORD, "reason": e.reason},
        )
    return UserRead.model_validate(created_user)


@public_auth_router.post(
    "/forgot-password",
    name="reset:forgot_password",
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("3/hour")
async def forgot_password(
    request: Request,
    email: EmailStr = Body(..., embed=True),
    user_manager: UserManager = Depends(get_user_manager),
):
    """Always 202 — never reveals whether the address exists (no enumeration)."""
    try:
        user = await user_manager.get_by_email(email)
    except fu_exc.UserNotExists:
        return None
    try:
        await user_manager.forgot_password(user, request)
    except fu_exc.UserInactive:
        pass
    return None


RESET_PASSWORD_RESPONSES = {
    status.HTTP_400_BAD_REQUEST: {
        "model": ErrorModel,
        "content": {
            "application/json": {
                "examples": {
                    ErrorCode.RESET_PASSWORD_BAD_TOKEN: {
                        "value": {"detail": ErrorCode.RESET_PASSWORD_BAD_TOKEN}
                    },
                    ErrorCode.RESET_PASSWORD_INVALID_PASSWORD: {
                        "value": {
                            "detail": {
                                "code": ErrorCode.RESET_PASSWORD_INVALID_PASSWORD,
                                "reason": "Password should be at least 3 characters",
                            }
                        },
                    },
                }
            }
        },
    },
}

@public_auth_router.post(
    "/reset-password",
    name="reset:reset_password",
    responses=RESET_PASSWORD_RESPONSES,
)
@limiter.limit("10/hour")
async def reset_password(
    request: Request,
    token: str = Body(...),
    password: str = Body(...),
    user_manager: UserManager = Depends(get_user_manager),
):
    """Consumes a tv-stamped reset token; success bumps token_version."""
    try:
        await user_manager.reset_password(token, password, request)
    except (
        fu_exc.InvalidResetPasswordToken,
        fu_exc.UserNotExists,
        fu_exc.UserInactive,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorCode.RESET_PASSWORD_BAD_TOKEN,
        )
    except fu_exc.InvalidPasswordException as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": ErrorCode.RESET_PASSWORD_INVALID_PASSWORD,
                "reason": e.reason,
            },
        )


def _user_limit_key(request: Request) -> str:
    """Account changes are keyed on the authenticated user, not IP (a shared
    NAT office must not lock everyone out of their own account settings)."""
    return f"user:{getattr(request.state, 'user_key', '')}"


async def current_user_keyed(request: Request, user: User = Depends(current_active_user)) -> User:
    # Resolved before the limiter decorator runs → request.state carries the key.
    request.state.user_key = str(user.id)
    return user


class AccountUpdate(BaseModel):
    email: EmailStr | None = None
    new_password: str | None = None
    current_password: str | None = None


@auth_router.patch("/account", response_model=UserRead)
@limiter.limit("10/hour", key_func=_user_limit_key)
async def update_account(
    request: Request,
    payload: AccountUpdate,
    user: User = Depends(current_user_keyed),
    user_manager: UserManager = Depends(get_user_manager),
):
    """Authenticated account self-service (PATCH /api/v1/auth/account).

    Email and password changes both require current_password — an
    account-takeover guard, since a hijacked session alone must not be able
    to lock the owner out. Either change bumps token_version: every session
    cookie (all devices) is revoked and the FE must re-login.
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

    if updates:  # password or email actually changed → kill every session JWT
        await user_manager.user_db.update(
            user, {"token_version": int(user.token_version or 0) + 1}
        )
    return user


# --- Social login (Google / GitHub) ------------------------------------------
#
# Sanctioned fastapi-users extension: get_oauth_router() handles the
# authorize→callback→account-link flow; we only supply clients + env config.
# Providers are opt-in via env keys — unconfigured ones are simply not routed
# and not advertised (GET /api/v1/auth/providers).

FFAA_PUBLIC_URL = os.environ.get("FFAA_PUBLIC_URL", "http://localhost:5173")

def build_oauth_routers() -> list[tuple[APIRouter, str]]:
    """One (router, provider-name) pair per provider with env keys set."""
    from httpx_oauth.clients.github import GitHubOAuth2
    from httpx_oauth.clients.google import GoogleOAuth2

    pairs: list[tuple[APIRouter, str]] = []
    providers = [
        ("google", GoogleOAuth2, "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
        ("github", GitHubOAuth2, "GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET"),
    ]
    for name, client_cls, env_id, env_secret in providers:
        client_id = os.environ.get(env_id, "")
        client_secret = os.environ.get(env_secret, "")
        if not (client_id and client_secret):
            continue
        client = client_cls(client_id, client_secret)
        router = fastapi_users.get_oauth_router(
            client,
            auth_backend,
            FFAA_SECRET,  # state_secret — CSRF JWT for the oauth round-trip
            redirect_url=f"{FFAA_PUBLIC_URL}/api/v1/auth/{name}/callback",
            associate_by_email=True,  # link to existing account with same email
            is_verified_by_default=True,  # Google/GitHub verify emails upstream
        )
        pairs.append((router, name))
    return pairs


def configured_oauth_providers() -> list[str]:
    return [
        name
        for name, _cls, env_id, env_secret in [
            ("google", None, "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
            ("github", None, "GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET"),
        ]
        if os.environ.get(env_id) and os.environ.get(env_secret)
    ]
