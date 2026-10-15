"""Portal credentials: only digests persist; expensive password work stays off the loop."""

import asyncio
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import ActivationToken, CustomerSession, CustomerUser, Tenant

COOKIE_NAME = "portal_session"
_PASSWORDS = PasswordHasher(type=Type.ID)
_AUTH_LIMIT_SCRIPT = """
for i = 1, #KEYS do
    local count = redis.call('INCR', KEYS[i])
    if count == 1 then redis.call('EXPIRE', KEYS[i], 60) end
    if count > 6 then return 0 end
end
return 1
"""


def failure(status: int, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers={"Cache-Control": "no-store"})


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def csrf_token(raw_session: str) -> str:
    return hmac.new(
        settings.api_key_hmac_secret.encode(),
        ("portal-csrf:" + raw_session).encode(),
        hashlib.sha256,
    ).hexdigest()


def unexpired(expires_at: datetime) -> bool:
    # SQLite test fixtures do not preserve timezone metadata.
    aware = expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at
    return aware > datetime.now(UTC)


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin or origin not in settings.customer_allowed_origins:
        raise failure(403, "invalid portal origin")


async def rate_limit(request: Request, identity: str) -> None:
    # Peer address only: forwarded headers are untrusted input.
    peer = request.client.host if request.client else "unknown"
    keys = ["portal-auth:ip:" + digest(peer), "portal-auth:identity:" + digest(identity)]
    try:
        allowed = int(await request.app.state.redis.eval(_AUTH_LIMIT_SCRIPT, 2, *keys))
    except Exception:
        raise failure(503, "authentication temporarily unavailable") from None
    if allowed != 1:
        error = failure(429, "too many authentication attempts")
        error.headers = {"Cache-Control": "no-store", "Retry-After": "60"}
        raise error


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _PASSWORDS.hash(secrets.token_urlsafe(32))


def _verify(password_hash: str | None, password: str) -> bool:
    try:
        valid = _PASSWORDS.verify(password_hash or _dummy_hash(), password)
    except (VerificationError, InvalidHashError):
        return False
    return bool(valid and password_hash)


async def new_session(session: AsyncSession, user: CustomerUser) -> str:
    raw = secrets.token_urlsafe(32)
    session.add(
        CustomerSession(
            user_id=user.id,
            token_hash=digest(raw),
            csrf_hash=digest(csrf_token(raw)),
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.customer_session_ttl_seconds),
        )
    )
    await session.flush()
    return raw


async def login(
    session: AsyncSession, email: str, password: str
) -> tuple[CustomerUser, Tenant, str]:
    # Warm on every path so the first absent email does not incur an extra hash alone.
    await asyncio.to_thread(_dummy_hash)
    user = await session.scalar(
        select(CustomerUser)
        .where(CustomerUser.email == email.strip().lower())
        .execution_options(populate_existing=True)
    )
    valid = await asyncio.to_thread(_verify, user.password_hash if user else None, password)
    if user is not None:
        await session.refresh(user)
    tenant = await session.get(Tenant, user.tenant_id, populate_existing=True) if user else None
    if not valid or user is None or not user.active or tenant is None or not tenant.active:
        raise failure(401, "invalid credentials")
    raw = await new_session(session, user)
    await session.commit()
    return user, tenant, raw


async def activate(
    session: AsyncSession, raw_token: str, password: str
) -> tuple[CustomerUser, Tenant, str]:
    # Discover ownership, then lock in tenant -> user -> token order, like issuance.
    candidate = await session.scalar(
        select(ActivationToken).where(ActivationToken.token_hash == digest(raw_token))
    )
    user = await session.get(CustomerUser, candidate.user_id) if candidate else None
    if candidate is None or user is None:
        raise failure(401, "invalid activation token")
    tenant = await session.scalar(
        select(Tenant).where(Tenant.id == user.tenant_id).with_for_update()
    )
    user = await session.scalar(
        select(CustomerUser)
        .where(CustomerUser.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    token = await session.scalar(
        select(ActivationToken)
        .where(ActivationToken.id == candidate.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        tenant is None
        or not tenant.active
        or user is None
        or user.active
        or token is None
        or token.used_at is not None
        or not unexpired(token.expires_at)
    ):
        raise failure(401, "invalid activation token")
    user.password_hash = await asyncio.to_thread(_PASSWORDS.hash, password)
    user.active = True
    token.used_at = datetime.now(UTC)
    raw = await new_session(session, user)
    await session.commit()
    return user, tenant, raw


async def issue_activation(
    session: AsyncSession, tenant_id: UUID, email: str
) -> tuple[str, datetime, CustomerUser]:
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if tenant is None:
        raise failure(404, "tenant not found")
    if not tenant.active:
        raise failure(409, "tenant is inactive")
    user = await session.scalar(
        select(CustomerUser)
        .where(CustomerUser.tenant_id == tenant.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is not None and user.active:
        raise failure(409, "customer already activated")
    if user is not None and user.email != email:
        raise failure(409, "customer email does not match")
    if not settings.customer_allowed_origins:
        raise failure(503, "portal origin unavailable")
    if user is None:
        user = CustomerUser(tenant_id=tenant.id, email=email)
        session.add(user)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise failure(409, "customer account conflict") from None
    now = datetime.now(UTC)
    await session.execute(
        update(ActivationToken)
        .where(ActivationToken.user_id == user.id, ActivationToken.used_at.is_(None))
        .values(used_at=now)
    )
    raw = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=settings.activation_ttl_seconds)
    session.add(ActivationToken(user_id=user.id, token_hash=digest(raw), expires_at=expires_at))
    await session.flush()
    url = settings.customer_allowed_origins[0].rstrip("/") + "/portal/activate#token=" + raw
    return url, expires_at, user
