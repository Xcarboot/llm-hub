"""Exercise the shipped app, not a separately assembled collection of routers."""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from tests.test_customer_auth import FakeRedis

ORIGIN = "https://localhost"
SECRET = "entry-test-password-do-not-reflect"


@pytest.fixture
async def entry_client(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "customer_allowed_origins", [ORIGIN])
    monkeypatch.setattr(app.state, "redis", FakeRedis(), raising=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    # Restore all shared application state even if a request/assertion fails.
    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = override_session
    try:
        # ASGITransport intentionally does not start lifespan or connect to real services.
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.mark.parametrize("path", ["/portal/session", "/portal/balance", "/portal/payments"])
async def test_actual_app_registers_all_customer_routers(
    entry_client: AsyncClient, path: str
) -> None:
    response = await entry_client.get(path)
    assert response.status_code == 401, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "invalid portal session"}


@pytest.mark.parametrize(
    ("body", "field", "private_values"),
    [
        (
            {"email": "entry@example.test", "password": {"secret": SECRET}},
            "password",
            [SECRET],
        ),
        (
            {"email": "entry@example.test", "password": SECRET * 40},
            "password",
            [SECRET, SECRET * 40],
        ),
        (
            {"email": "invalid-email-private-marker", "password": SECRET},
            "email",
            ["invalid-email-private-marker", SECRET],
        ),
        (
            {"email": {"private": "email-private-marker"}, "password": SECRET},
            "email",
            ["email-private-marker", SECRET],
        ),
        (
            {"email": "entry@example.test", "password": SECRET, "unexpected": SECRET},
            "unexpected",
            [SECRET],
        ),
    ],
    ids=["password-type", "password-length", "email-validator", "email-type", "extra-field"],
)
async def test_actual_app_redacts_login_validation_errors(
    entry_client: AsyncClient,
    body: dict[str, Any],
    field: str,
    private_values: list[str],
) -> None:
    response = await entry_client.post("/portal/login", json=body, headers={"Origin": ORIGIN})
    assert response.status_code == 422, response.text
    assert response.headers["cache-control"] == "no-store"
    for private in private_values:
        assert private not in response.text
    assert "entry@example.test" not in response.text
    errors = response.json()["detail"]
    assert errors and any(error["loc"] == ["body", field] for error in errors)
    for error in errors:
        assert "input" not in error and "ctx" not in error
        assert set(error) == {"type", "loc", "msg"}
        assert error["msg"] == "Invalid request field"


async def test_actual_app_login_uses_fake_redis_and_is_noncacheable(
    entry_client: AsyncClient,
) -> None:
    response = await entry_client.post(
        "/portal/login",
        json={"email": "absent@example.test", "password": SECRET},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "invalid credentials"}
    assert response.headers["cache-control"] == "no-store"
    assert SECRET not in response.text and "absent@example.test" not in response.text
    assert app.state.redis.calls
    assert SECRET not in repr(app.state.redis.calls)
    assert "absent@example.test" not in repr(app.state.redis.calls)


async def test_actual_app_redacts_activation_token_and_password(entry_client: AsyncClient) -> None:
    token = "private-activation-token-marker"
    response = await entry_client.post(
        "/portal/activate",
        json={"token": {"value": token}, "password": "tiny-secret"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 422, response.text
    assert response.headers["cache-control"] == "no-store"
    assert token not in response.text and "tiny-secret" not in response.text
    assert {error["loc"][-1] for error in response.json()["detail"]} == {"token", "password"}
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])


async def test_actual_app_activation_requires_admin(entry_client: AsyncClient) -> None:
    response = await entry_client.post(
        f"/admin/tenants/{uuid4()}/customer-activation",
        json={"email": "entry@example.test"},
    )
    assert response.status_code == 401, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "invalid admin token"}
