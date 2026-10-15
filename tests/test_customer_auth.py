from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.routes import customer_account, customer_auth, gateway
from app.core.config import settings
from app.db.models import ActivationToken, CustomerSession, CustomerUser, Tenant
from app.db.session import get_session
from app.services.customer_auth import COOKIE_NAME, digest

ORIGIN = "https://portal.example.test"
PASSWORD = "test-only-strong-password"


class FakeRedis:
    def __init__(self) -> None:
        self.allowed = 1
        self.unavailable = False
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: Any) -> int:
        self.calls.append(args)
        if self.unavailable:
            raise ConnectionError("test Redis failure")
        return self.allowed


@pytest.fixture
async def portal_client(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "customer_allowed_origins", [ORIGIN])
    monkeypatch.setattr(settings, "customer_cookie_secure", True)
    app = FastAPI()
    app.include_router(customer_auth.router)
    app.include_router(customer_account.router)
    app.include_router(gateway.router)
    app.state.redis = FakeRedis()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        yield client


async def add_customer_tenant(session: AsyncSession, name: str = "customer") -> Tenant:
    tenant = Tenant(name=name)
    session.add(tenant)
    await session.commit()
    return tenant


async def admin_activation(client: AsyncClient, tenant: Tenant, email: str) -> str:
    response = await client.post(
        f"/admin/tenants/{tenant.id}/customer-activation", json={"email": email}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    parsed = urlsplit(response.json()["activation_url"])
    assert parsed.scheme + "://" + parsed.netloc == ORIGIN
    assert not parsed.query
    return parse_qs(parsed.fragment)["token"][0]


async def activated_customer(
    session: AsyncSession, client: AsyncClient, name: str = "customer"
) -> tuple[Tenant, dict[str, str]]:
    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[require_admin] = lambda: None
    tenant = await add_customer_tenant(session, name)
    token = await admin_activation(client, tenant, name + "@example.test")
    response = await client.post(
        "/portal/activate", json={"token": token, "password": PASSWORD}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    return tenant, {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


async def test_activation_login_profile_and_no_hashes(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    tenant, _ = await activated_customer(session, portal_client)
    profile = await portal_client.get("/portal/session")
    assert set(profile.json()) == {"id", "tenant_id", "email", "tenant_name", "csrf_token"}
    assert profile.json()["tenant_id"] == str(tenant.id)
    user = await session.scalar(select(CustomerUser))
    assert (
        user and user.active and user.password_hash and user.password_hash.startswith("$argon2id$")
    )
    db_cookie = await session.scalar(select(CustomerSession))
    raw_cookie = portal_client.cookies[COOKIE_NAME]
    assert db_cookie and db_cookie.token_hash == digest(raw_cookie)
    assert db_cookie.csrf_hash == digest(profile.json()["csrf_token"])
    assert raw_cookie not in profile.text and user.password_hash not in profile.text
    login = await portal_client.post(
        "/portal/login",
        json={"email": " CUSTOMER@EXAMPLE.TEST ", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    for flag in ("httponly", "samesite=lax", "path=/", "secure", "max-age="):
        assert flag in cookie
    assert login.headers["cache-control"] == "no-store"
    assert (await portal_client.get("/portal/session")).json() == login.json()


async def test_credentials_errors_are_uniform(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    await activated_customer(session, portal_client)
    replies = []
    for email in ("customer@example.test", "absent@example.test"):
        response = await portal_client.post(
            "/portal/login", json={"email": email, "password": "wrong"}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        replies.append(response.json())
    assert replies[0] == replies[1] == {"detail": "invalid credentials"}


@pytest.mark.parametrize("path", ["/portal/login", "/portal/activate"])
@pytest.mark.parametrize("origin", [None, "https://attacker.example", "null"])
async def test_login_csrf_origin_required(
    portal_client: AsyncClient, path: str, origin: str | None
) -> None:
    body = {"password": PASSWORD}
    body.update({"email": "customer@example.test"} if path.endswith("login") else {"token": "x"})
    response = await portal_client.post(
        path, json=body, headers={"Origin": origin} if origin else {}
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


async def test_resend_expire_reuse_and_active_account_not_reset(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    app = portal_client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[require_admin] = lambda: None
    tenant = await add_customer_tenant(session)
    old = await admin_activation(portal_client, tenant, "customer@example.test")
    new = await admin_activation(portal_client, tenant, "customer@example.test")
    expired = await session.scalar(
        select(ActivationToken).where(ActivationToken.token_hash == digest(new))
    )
    assert expired
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    for token in (old, new):
        response = await portal_client.post(
            "/portal/activate",
            json={"token": token, "password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 401
    valid = await admin_activation(portal_client, tenant, "customer@example.test")
    for status in (200, 401):
        response = await portal_client.post(
            "/portal/activate",
            json={"token": valid, "password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == status
    user = await session.scalar(select(CustomerUser))
    assert user
    password_hash = user.password_hash
    for email in ("customer@example.test", "changed@example.test"):
        response = await portal_client.post(
            f"/admin/tenants/{tenant.id}/customer-activation", json={"email": email}
        )
        assert response.status_code == 409
    assert user.password_hash == password_hash and user.email == "customer@example.test"


@pytest.mark.parametrize(
    "headers", [{}, {"Origin": ORIGIN}, {"Origin": ORIGIN, "X-CSRF-Token": "wrong"}]
)
async def test_logout_requires_csrf(
    session: AsyncSession, portal_client: AsyncClient, headers: dict[str, str]
) -> None:
    await activated_customer(session, portal_client)
    assert (await portal_client.post("/portal/logout", headers=headers)).status_code == 403
    assert (await portal_client.get("/portal/session")).status_code == 200


async def test_logout_and_expired_sessions(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    _, headers = await activated_customer(session, portal_client)
    raw = portal_client.cookies[COOKIE_NAME]
    response = await portal_client.post("/portal/logout", headers=headers)
    assert response.status_code == 204 and not response.content
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert (await portal_client.get("/portal/session")).status_code == 401
    portal_client.cookies.set(COOKIE_NAME, raw)
    assert (await portal_client.get("/portal/session")).status_code == 401
    await portal_client.post(
        "/portal/login",
        json={"email": "customer@example.test", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    db_cookie = await session.scalar(
        select(CustomerSession).where(CustomerSession.revoked.is_(False))
    )
    assert db_cookie
    db_cookie.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    assert (await portal_client.get("/portal/session")).status_code == 401


@pytest.mark.parametrize("target", ["user", "tenant"])
async def test_active_checked_every_request(
    session: AsyncSession, portal_client: AsyncClient, target: str
) -> None:
    tenant, _ = await activated_customer(session, portal_client)
    user = await session.scalar(select(CustomerUser))
    assert user
    (user if target == "user" else tenant).active = False
    await session.commit()
    assert (await portal_client.get("/portal/balance")).status_code == 401
    response = await portal_client.post(
        "/portal/login",
        json={"email": user.email, "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 401


async def test_redis_fail_closed_hashed_identifiers_and_admin_authorization(
    portal_client: AsyncClient,
) -> None:
    app = portal_client._transport.app  # type: ignore[attr-defined]
    redis = app.state.redis
    body = {"email": "customer@example.test", "password": PASSWORD}
    redis.unavailable = True
    response = await portal_client.post("/portal/login", json=body, headers={"Origin": ORIGIN})
    assert response.status_code == 503
    redis.unavailable = False
    redis.allowed = 0
    response = await portal_client.post(
        "/portal/login", json=body, headers={"Origin": ORIGIN, "X-Forwarded-For": "spoof"}
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert "customer@example.test" not in repr(redis.calls)
    assert "spoof" not in repr(redis.calls) and PASSWORD not in repr(redis.calls)
    response = await portal_client.post(
        "/admin/tenants/00000000-0000-0000-0000-000000000001/customer-activation",
        json={"email": "customer@example.test"},
    )
    assert response.status_code == 401
