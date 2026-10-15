"""SQLite unit/API coverage; no provider requests or application DB access."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import customer_payments as routes
from app.core.config import Settings, settings
from app.core.errors import ConflictError, InsufficientBalanceError
from app.db.models import (
    AccountKind,
    AuditEvent,
    CustomerSession,
    CustomerUser,
    FundLot,
    GatewayRequest,
    LedgerAccount,
    LedgerEntry,
    LedgerTransfer,
    PaymentOperation,
    PaymentOrder,
    RefundRequest,
    Tenant,
    TransferKind,
)
from app.db.session import get_session
from app.services import customer_payments as payments
from app.services import funds, ledger
from app.services.customer_auth import COOKIE_NAME, csrf_token, digest


@dataclass
class Customer:
    tenant: Tenant
    user: CustomerUser
    raw_session: str
    csrf: str


@pytest.fixture
def mock_config(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "payment_mode", "mock")
    monkeypatch.setattr(settings, "api_key_hmac_secret", "unit-test-hmac-not-a-real-secret" * 2)
    monkeypatch.setattr(settings, "admin_token", "unit-test-admin-not-a-real-secret" * 2)
    monkeypatch.setattr(settings, "customer_allowed_origins", ["http://localhost:3000"])
    return settings


async def add_customer(session: AsyncSession) -> Customer:
    tenant = Tenant(name=f"payments-{uuid4().hex}")
    session.add(tenant)
    await session.flush()
    await ledger.create_tenant_accounts(session, tenant.id)
    await ledger.ensure_platform_accounts(session)
    user = CustomerUser(
        tenant_id=tenant.id,
        email=f"{uuid4().hex}@example.com",
        active=True,
        password_hash="private-password-hash",
    )
    session.add(user)
    await session.flush()
    raw = f"unit-session-{uuid4().hex}"
    csrf = csrf_token(raw)
    session.add(
        CustomerSession(
            user_id=user.id,
            token_hash=digest(raw),
            csrf_hash=digest(csrf),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await session.commit()
    return Customer(tenant=tenant, user=user, raw_session=raw, csrf=csrf)


@pytest.fixture
async def customer(session: AsyncSession, mock_config: Settings) -> Customer:
    del mock_config
    return await add_customer(session)


@pytest.fixture
async def client(session: AsyncSession, customer: Customer) -> AsyncIterator[AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        try:
            yield session
        finally:
            # API errors correctly roll back (and expire ORM rows). This fixture
            # deliberately shares one session; refresh its test-held references
            # before the test resumes, unlike real independent request sessions.
            for row in list(session.identity_map.values()):
                await session.refresh(row)

    application = FastAPI()
    application.include_router(routes.router)
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://localhost"
    ) as http:
        http.cookies.set(COOKIE_NAME, customer.raw_session)
        yield http


def headers(customer: Customer) -> dict[str, str]:
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": customer.csrf}


async def paid(
    session: AsyncSession, customer: Customer, config: Settings, amount: int = 100_000
) -> PaymentOrder:
    row = await payments.create_payment(
        session,
        customer.tenant.id,
        str(amount),
        uuid4().hex,
        config,
        actor=f"customer:{customer.user.id}",
    )
    await payments.confirm_mock_payment(
        session, customer.tenant.id, row.id, config, actor=f"customer:{customer.user.id}"
    )
    await session.commit()
    return row


async def refund(
    session: AsyncSession,
    customer: Customer,
    order: PaymentOrder,
    config: Settings,
    amount: int = 30_000,
    key: str | None = None,
) -> RefundRequest:
    row = await payments.create_refund(
        session,
        customer.tenant.id,
        order.id,
        str(amount),
        "unused funds",
        key or uuid4().hex,
        config,
        actor=f"customer:{customer.user.id}",
    )
    await session.commit()
    return row


async def conserved(session: AsyncSession, customer: Customer) -> None:
    await funds.assert_source_totals(session, customer.tenant.id)
    totals = (
        await session.execute(
            select(LedgerEntry.transfer_id, func.sum(LedgerEntry.amount_micro_cny)).group_by(
                LedgerEntry.transfer_id
            )
        )
    ).all()
    assert all(total == 0 for _, total in totals)
    lots = (
        await session.scalars(select(FundLot).where(FundLot.tenant_id == customer.tenant.id))
    ).all()
    for lot in lots:
        assert lot.credited_micro_cny == sum(
            (
                lot.available_micro_cny,
                lot.call_frozen_micro_cny,
                lot.refund_frozen_micro_cny,
                lot.consumed_micro_cny,
                lot.refunded_micro_cny,
            )
        )
        assert 0 <= lot.refunded_micro_cny <= lot.credited_micro_cny


@pytest.mark.parametrize(
    "environment,mode,enabled",
    [
        ("development", "mock", True),
        ("test", "mock", True),
        ("production", "mock", False),
        ("staging", "mock", False),
        ("development", "disabled", False),
        ("development", "alipay_sandbox", False),
    ],
)
def test_config_is_explicit_and_fail_closed(environment: str, mode: str, enabled: bool) -> None:
    config = Settings(_env_file=None, app_env=environment, payment_mode=mode)
    result = payments.payment_config(config)
    assert set(result) == {"mode", "enabled", "mock", "notice"}
    assert result["mode"] == mode and result["enabled"] is enabled and result["mock"] is enabled
    assert result["notice"]
    if mode == "alipay_sandbox":
        assert "尚未接通" in result["notice"]


async def test_create_confirm_and_replay_are_single_credit(
    session: AsyncSession,
    client: AsyncClient,
    customer: Customer,
) -> None:
    config = await client.get("/portal/payments/config")
    assert config.json()["mock"] is True and "不是真实支付" in config.json()["notice"]
    body = {"amount_micro_cny": "0100000", "idempotency_key": "create-once"}
    first = await client.post("/portal/payments", json=body, headers=headers(customer))
    assert first.status_code == 200 and first.json()["status"] == "PENDING"
    order_id = first.json()["id"]
    duplicate = await client.post("/portal/payments", json=body, headers=headers(customer))
    assert duplicate.json()["id"] == order_id
    conflicting = await client.post(
        "/portal/payments", json={**body, "amount_micro_cny": "200000"}, headers=headers(customer)
    )
    assert conflicting.status_code == 409
    assert await session.scalar(select(func.count()).select_from(LedgerTransfer)) == 0
    for _ in range(2):
        result = await client.post(
            f"/portal/payments/{order_id}/mock-confirm", headers=headers(customer)
        )
        assert result.status_code == 200 and result.json()["status"] == "PAID"
        assert (
            result.json()["amount_micro_cny"] == result.json()["refundable_micro_cny"] == "100000"
        )
        assert result.json()["refundable_cents"] == "10"
    assert await session.scalar(select(func.count()).select_from(LedgerTransfer)) == 1
    assert await session.scalar(select(func.sum(FundLot.credited_micro_cny))) == 100_000
    actions = (await session.scalars(select(AuditEvent.action))).all()
    assert actions.count("payment.mock.create") == actions.count("payment.mock.confirm") == 1
    assert all(
        event.detail["mode"] == "mock"
        for event in (await session.scalars(select(AuditEvent))).all()
    )
    await conserved(session, customer)


async def test_owner_404_and_lists_are_tenant_scoped(
    session: AsyncSession,
    client: AsyncClient,
    customer: Customer,
    mock_config: Settings,
) -> None:
    own = await paid(session, customer, mock_config)
    own_refund = await refund(session, customer, own, mock_config)
    other = await add_customer(session)
    foreign = await paid(session, other, mock_config)
    foreign_refund = await refund(session, other, foreign, mock_config)
    for path in (f"/portal/payments/{foreign.id}", f"/portal/refunds/{foreign_refund.id}"):
        response = await client.get(path)
        assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    assert (
        await client.post(f"/portal/payments/{foreign.id}/mock-confirm", headers=headers(customer))
    ).status_code == 404
    assert (
        await client.post(
            "/portal/refunds",
            json={
                "payment_order_id": str(foreign.id),
                "amount_micro_cny": "10000",
                "reason": "not mine",
                "idempotency_key": "foreign",
            },
            headers=headers(customer),
        )
    ).status_code == 404
    for path, expected in (("/portal/payments", own.id), ("/portal/refunds", own_refund.id)):
        result = (await client.get(path, params={"tenant_id": str(other.tenant.id)})).json()
        assert set(result) == {"items", "next_cursor", "has_more"}
        assert [item["id"] for item in result["items"]] == [str(expected)]
        assert all("tenant_id" not in item and "tenant" not in item for item in result["items"])
        assert other.user.email not in str(result) and "private-password-hash" not in str(result)
    await conserved(session, customer)
    await conserved(session, other)


async def test_portal_writes_require_real_session_origin_and_csrf(
    client: AsyncClient,
    customer: Customer,
) -> None:
    body = {"amount_micro_cny": "10000", "idempotency_key": "csrf"}
    for invalid_headers in (
        {},
        {"Origin": "http://localhost:3000"},
        {"Origin": "https://evil.invalid", "X-CSRF-Token": customer.csrf},
        {"Origin": "http://localhost:3000", "X-CSRF-Token": "wrong"},
    ):
        response = await client.post("/portal/payments", json=body, headers=invalid_headers)
        assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
    client.cookies.clear()
    assert (
        await client.get("/portal/payments", headers={"Authorization": "Bearer gw_fake"})
    ).status_code == 401


@pytest.mark.parametrize(
    "amount",
    [
        "0",
        "1",
        "10001",
        "-10000",
        "1.0",
        "1e4",
        "NaN",
        "1000010000",
        "9223372036854775808",
        10000,
        True,
        None,
    ],
)
async def test_api_rejects_invalid_amounts(
    client: AsyncClient, customer: Customer, amount: Any, session: AsyncSession
) -> None:
    result = await client.post(
        "/portal/payments",
        json={"amount_micro_cny": amount, "idempotency_key": "invalid"},
        headers=headers(customer),
    )
    assert result.status_code == 422
    assert await session.scalar(select(func.count()).select_from(PaymentOrder)) == 0


async def test_refund_fingerprint_and_partial_refunds_never_exceed_source(
    session: AsyncSession,
    customer: Customer,
    mock_config: Settings,
) -> None:
    order = await paid(session, customer, mock_config)
    first = await refund(session, customer, order, mock_config, 30_000, "refund-once")
    replay = await payments.create_refund(
        session,
        customer.tenant.id,
        order.id,
        "030000",
        "unused funds",
        "refund-once",
        mock_config,
        actor="test",
    )
    assert replay.id == first.id
    for amount, reason in (("40000", "unused funds"), ("30000", "changed reason")):
        with pytest.raises(ConflictError, match="different payload"):
            await payments.create_refund(
                session,
                customer.tenant.id,
                order.id,
                amount,
                reason,
                "refund-once",
                mock_config,
                actor="test",
            )
    approved = await payments.approve_refund(session, first.id, "approved", mock_config)
    assert approved.status == "SUCCEEDED"
    await session.commit()
    second = await refund(session, customer, order, mock_config, 70_000)
    await payments.approve_refund(session, second.id, "approved", mock_config)
    await session.commit()
    with pytest.raises(InsufficientBalanceError):
        await payments.create_refund(
            session,
            customer.tenant.id,
            order.id,
            "10000",
            "over limit",
            "over",
            mock_config,
            actor="test",
        )
    view = await payments.get_payment(session, customer.tenant.id, order.id)
    assert view["refunded_micro_cny"] == "100000" and view["refundable_micro_cny"] == "0"
    assert view["refund_frozen_micro_cny"] == "0"
    await conserved(session, customer)


async def test_approve_idempotency_and_reject_restores_funds(
    session: AsyncSession,
    client: AsyncClient,
    customer: Customer,
    mock_config: Settings,
) -> None:
    order = await paid(session, customer, mock_config)
    first = await refund(session, customer, order, mock_config)
    admin_headers = {"X-Admin-Token": settings.admin_token}
    url = f"/admin/refunds/{first.id}/approve"
    assert (await client.post(url, json={"reason": "ok"})).status_code == 401
    for _ in range(2):
        result = await client.post(url, json={"reason": "ok"}, headers=admin_headers)
        assert result.status_code == 200 and result.json()["status"] == "SUCCEEDED"
    assert (
        await client.post(url, json={"reason": "different"}, headers=admin_headers)
    ).status_code == 409
    assert await session.scalar(select(func.count()).select_from(PaymentOperation)) == 1
    assert await session.scalar(select(PaymentOperation.state)) == "DONE"
    assert (
        await session.scalar(
            select(func.count())
            .select_from(LedgerTransfer)
            .where(LedgerTransfer.kind == TransferKind.REFUND)
        )
        == 1
    )
    second = await refund(session, customer, order, mock_config, 70_000)
    rejected = await client.post(
        f"/admin/refunds/{second.id}/reject", json={"reason": "no"}, headers=admin_headers
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"
    result = (await client.get(f"/portal/payments/{order.id}")).json()
    assert (
        result["fund_available_micro_cny"] == "70000" and result["refund_frozen_micro_cny"] == "0"
    )
    assert (
        await client.post(
            f"/admin/refunds/{second.id}/reject", json={"reason": "no"}, headers=admin_headers
        )
    ).status_code == 409
    await conserved(session, customer)


async def test_refund_hold_blocks_calls_and_second_refund(
    session: AsyncSession,
    customer: Customer,
    mock_config: Settings,
) -> None:
    order = await paid(session, customer, mock_config)
    await refund(session, customer, order, mock_config, 100_000)
    with pytest.raises(InsufficientBalanceError):
        await payments.create_refund(
            session,
            customer.tenant.id,
            order.id,
            "10000",
            "again",
            "again",
            mock_config,
            actor="test",
        )
    async with session.begin_nested() as savepoint:
        gateway = GatewayRequest(
            tenant_id=customer.tenant.id, model="unit", stream=False, payload_hash="a" * 64
        )
        session.add(gateway)
        await session.flush()
        with pytest.raises(InsufficientBalanceError):
            await ledger.reserve(session, customer.tenant.id, gateway.id, 1, {}, 600)
        await savepoint.rollback()
    await conserved(session, customer)


async def test_call_frozen_consumed_and_manual_money_cannot_be_refunded(
    session: AsyncSession,
    customer: Customer,
    mock_config: Settings,
) -> None:
    order = await paid(session, customer, mock_config)
    gateway = GatewayRequest(
        tenant_id=customer.tenant.id, model="unit", stream=False, payload_hash="a" * 64
    )
    session.add(gateway)
    await session.flush()
    reservation = await ledger.reserve(session, customer.tenant.id, gateway.id, 80_000, {}, 600)
    with pytest.raises(InsufficientBalanceError):
        await payments.create_refund(
            session,
            customer.tenant.id,
            order.id,
            "30000",
            "held",
            "held",
            mock_config,
            actor="test",
        )
    await ledger.settle(session, reservation.id, 75_001, uuid4())
    await ledger.manual_recharge(session, customer.tenant.id, 1_000_000, "manual")
    view = await payments.get_payment(session, customer.tenant.id, order.id)
    assert view["fund_available_micro_cny"] == "24999" and view["refundable_micro_cny"] == "20000"
    await refund(session, customer, order, mock_config, 20_000)
    with pytest.raises(InsufficientBalanceError):
        await payments.create_refund(
            session,
            customer.tenant.id,
            order.id,
            "10000",
            "manual",
            "manual",
            mock_config,
            actor="test",
        )
    assert all(
        not lot.refundable
        for lot in (
            await session.scalars(select(FundLot).where(FundLot.payment_order_id.is_(None)))
        ).all()
    )
    await conserved(session, customer)


@pytest.mark.parametrize("status", ["UNKNOWN", "PROCESSING", "FAILED", "SUCCEEDED"])
async def test_nonpending_reject_never_releases(
    session: AsyncSession,
    customer: Customer,
    mock_config: Settings,
    status: str,
) -> None:
    order = await paid(session, customer, mock_config)
    row = await refund(session, customer, order, mock_config)
    row.status = status
    await session.flush()
    before = await payments.get_payment(session, customer.tenant.id, order.id)
    with pytest.raises(ConflictError, match="must not be released"):
        await payments.reject_refund(session, row.id, "unsafe")
    assert await payments.get_payment(session, customer.tenant.id, order.id) == before
    await conserved(session, customer)


@pytest.mark.parametrize(
    "environment,mode",
    [
        ("production", "mock"),
        ("staging", "mock"),
        ("development", "disabled"),
        ("development", "alipay_sandbox"),
    ],
)
async def test_mock_execution_fail_closed_and_preserves_pending_hold(
    session: AsyncSession,
    client: AsyncClient,
    customer: Customer,
    mock_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    mode: str,
) -> None:
    order = await paid(session, customer, mock_config)
    row = await refund(session, customer, order, mock_config)
    before = await payments.get_payment(session, customer.tenant.id, order.id)
    monkeypatch.setattr(settings, "app_env", environment)
    monkeypatch.setattr(settings, "payment_mode", mode)
    result = await client.get("/portal/payments/config")
    assert not result.json()["enabled"] and not result.json()["mock"]
    cases = [
        (
            "/portal/payments",
            {"amount_micro_cny": "10000", "idempotency_key": "off"},
            headers(customer),
        ),
        (f"/portal/payments/{order.id}/mock-confirm", None, headers(customer)),
        (
            "/portal/refunds",
            {
                "payment_order_id": str(order.id),
                "amount_micro_cny": "30000",
                "reason": "unused funds",
                "idempotency_key": row.idempotency_key,
            },
            headers(customer),
        ),
        (
            f"/admin/refunds/{row.id}/approve",
            {"reason": "off"},
            {"X-Admin-Token": settings.admin_token},
        ),
    ]
    for url, body, request_headers in cases:
        response = await client.post(url, json=body, headers=request_headers)
        assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert await payments.get_payment(session, customer.tenant.id, order.id) == before
    await session.refresh(row)
    assert row.status == "PENDING_APPROVAL"
    assert await session.scalar(select(func.count()).select_from(PaymentOperation)) == 0
    await conserved(session, customer)


async def test_sandbox_order_is_not_simulated_even_with_credentials(
    session: AsyncSession,
    client: AsyncClient,
    customer: Customer,
    mock_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = await paid(session, customer, mock_config)
    row = await refund(session, customer, order, mock_config)
    order.mode = "alipay_sandbox"
    order.trade_no = "sandbox-history-trade"
    await session.commit()
    monkeypatch.setattr(settings, "payment_mode", "alipay_sandbox")
    before = await payments.get_payment(session, customer.tenant.id, order.id)
    result = await client.post(
        f"/admin/refunds/{row.id}/approve",
        json={"reason": "approve"},
        headers={"X-Admin-Token": settings.admin_token},
    )
    assert result.status_code == 503 and "尚未接通" in result.json()["detail"]
    assert await payments.get_payment(session, customer.tenant.id, order.id) == before
    assert (
        await client.post(f"/portal/payments/{order.id}/mock-confirm", headers=headers(customer))
    ).status_code == 503
    await conserved(session, customer)


@pytest.mark.parametrize("broken", ["amount_micro_cny", "trade_no", "credit_transfer_id"])
async def test_paid_replay_checks_amount_trade_and_credit_invariants(
    session: AsyncSession,
    customer: Customer,
    mock_config: Settings,
    broken: str,
) -> None:
    order = await paid(session, customer, mock_config)
    setattr(
        order,
        broken,
        {"amount_micro_cny": 200_000, "trade_no": "wrong-trade", "credit_transfer_id": None}[
            broken
        ],
    )
    await session.flush()
    with pytest.raises(ConflictError):
        await payments.confirm_mock_payment(
            session, customer.tenant.id, order.id, mock_config, actor="test"
        )
    assert await session.scalar(select(func.count()).select_from(LedgerTransfer)) == 1


async def test_transaction_failure_rolls_back_credit_and_refund_finalization(
    session: AsyncSession,
    customer: Customer,
    mock_config: Settings,
) -> None:
    order = await payments.create_payment(
        session, customer.tenant.id, "100000", "atomic", mock_config, actor="test"
    )
    await session.commit()
    order_id = order.id
    async with session.begin_nested() as savepoint:
        with patch.object(
            ledger, "_post_transfer", new=AsyncMock(side_effect=RuntimeError("fault"))
        ):
            with pytest.raises(RuntimeError, match="fault"):
                await payments.confirm_mock_payment(
                    session, customer.tenant.id, order_id, mock_config, actor="test"
                )
        await savepoint.rollback()
    await session.refresh(order)
    assert order.status == "PENDING"
    assert await session.scalar(select(func.count()).select_from(FundLot)) == 0
    await payments.confirm_mock_payment(
        session, customer.tenant.id, order_id, mock_config, actor="test"
    )
    await session.commit()
    row = await refund(session, customer, order, mock_config)
    before = await payments.get_payment(session, customer.tenant.id, order_id)
    refund_id = row.id
    async with session.begin_nested() as savepoint:
        with patch.object(
            ledger, "_post_transfer", new=AsyncMock(side_effect=RuntimeError("fault"))
        ):
            with pytest.raises(RuntimeError, match="fault"):
                await payments.approve_refund(session, refund_id, "atomic", mock_config)
        await savepoint.rollback()
    await session.refresh(row)
    assert row.status == "PENDING_APPROVAL"
    assert await session.scalar(select(func.count()).select_from(PaymentOperation)) == 0
    assert await payments.get_payment(session, customer.tenant.id, order_id) == before
    await conserved(session, customer)


async def test_admin_metadata_filters_pagination_and_bad_cursor(
    session: AsyncSession,
    client: AsyncClient,
    customer: Customer,
    mock_config: Settings,
) -> None:
    for _ in range(3):
        order = await paid(session, customer, mock_config)
        await refund(session, customer, order, mock_config)
    other = await add_customer(session)
    await paid(session, other, mock_config)
    admin_headers = {"X-Admin-Token": settings.admin_token}
    for path, status in (("/admin/payments", "PAID"), ("/admin/refunds", "PENDING_APPROVAL")):
        assert (await client.get(path)).status_code == 401
        ids = []
        cursor = None
        for page in range(3):
            params = {"limit": "1", "tenant_id": str(customer.tenant.id), "status": status}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(path, params=params, headers=admin_headers)
            assert response.status_code == 200
            result = response.json()
            assert len(result["items"]) == 1
            item = result["items"][0]
            ids.append(item["id"])
            assert item["tenant"] == {
                "id": str(customer.tenant.id),
                "name": customer.tenant.name,
                "email": customer.user.email,
            }
            assert "private-password-hash" not in str(result) and "payload_hash" not in str(result)
            assert result["has_more"] is (page < 2)
            cursor = result["next_cursor"]
        assert len(set(ids)) == 3 and cursor is None
    for value in ("bad!", "e30", payments._cursor_encode(datetime.now(UTC), uuid4()) + "!"):
        assert (await client.get("/portal/payments", params={"cursor": value})).status_code == 422
    assert (await client.get("/portal/payments", params={"limit": 101})).status_code == 422
    await conserved(session, customer)


async def test_maximum_mock_amount_and_forbidden_customer_tenant_field(
    client: AsyncClient,
    customer: Customer,
) -> None:
    body = {"amount_micro_cny": "1000000000", "idempotency_key": "maximum"}
    assert (
        await client.post("/portal/payments", json=body, headers=headers(customer))
    ).status_code == 200
    assert (
        await client.post(
            "/portal/payments", json={**body, "tenant_id": str(uuid4())}, headers=headers(customer)
        )
    ).status_code == 422


async def test_refund_api_freezes_once_and_checks_all_payload_fields(
    session: AsyncSession,
    client: AsyncClient,
    customer: Customer,
    mock_config: Settings,
) -> None:
    order = await paid(session, customer, mock_config)
    another = await paid(session, customer, mock_config)
    body = {
        "payment_order_id": str(order.id),
        "amount_micro_cny": "30000",
        "reason": "unused",
        "idempotency_key": "api-refund",
    }
    assert (await client.post("/portal/refunds", json=body)).status_code == 403
    first = await client.post("/portal/refunds", json=body, headers=headers(customer))
    assert first.status_code == 200 and first.json()["status"] == "PENDING_APPROVAL"
    refund_id = first.json()["id"]
    duplicate = await client.post("/portal/refunds", json=body, headers=headers(customer))
    assert duplicate.status_code == 200 and duplicate.json()["id"] == refund_id
    for changed in (
        {"amount_micro_cny": "40000"},
        {"reason": "different"},
        {"payment_order_id": str(another.id)},
    ):
        assert (
            await client.post(
                "/portal/refunds", json={**body, **changed}, headers=headers(customer)
            )
        ).status_code == 409
    for changed in (
        {"amount_micro_cny": "0"},
        {"amount_micro_cny": "1000010000"},
        {"amount_micro_cny": "1"},
        {"reason": " "},
        {"idempotency_key": " "},
        {"tenant_id": str(uuid4())},
    ):
        assert (
            await client.post(
                "/portal/refunds", json={**body, **changed}, headers=headers(customer)
            )
        ).status_code == 422
    result = (await client.get(f"/portal/refunds/{refund_id}")).json()
    assert result["fund_available_micro_cny"] == "70000"
    assert result["refund_frozen_micro_cny"] == "30000"
    assert (await client.get("/portal/refunds")).json()["items"][0]["id"] == refund_id
    assert await session.scalar(select(func.count()).select_from(RefundRequest)) == 1
    await conserved(session, customer)


@pytest.mark.parametrize("status", ["UNKNOWN", "PROCESSING"])
async def test_unknown_or_processing_approval_keeps_hold(
    session: AsyncSession,
    client: AsyncClient,
    customer: Customer,
    mock_config: Settings,
    status: str,
) -> None:
    order = await paid(session, customer, mock_config)
    row = await refund(session, customer, order, mock_config)
    row.status = status
    await session.commit()
    before = await payments.get_payment(session, customer.tenant.id, order.id)
    response = await client.post(
        f"/admin/refunds/{row.id}/approve",
        json={"reason": "unsafe"},
        headers={"X-Admin-Token": settings.admin_token},
    )
    assert response.status_code == 409
    assert await payments.get_payment(session, customer.tenant.id, order.id) == before
    assert await session.scalar(select(func.count()).select_from(PaymentOperation)) == 0
    await conserved(session, customer)


async def test_old_ledger_credit_stays_nonrefundable_and_refund_account_is_created(
    session: AsyncSession,
    customer: Customer,
    mock_config: Settings,
) -> None:
    tenant = Tenant(name=f"legacy-payments-{uuid4().hex}")
    session.add(tenant)
    await session.flush()
    session.add_all(
        [
            LedgerAccount(
                tenant_id=tenant.id, kind=kind, scope=f"tenant:{tenant.id}:{kind.value.lower()}"
            )
            for kind in (AccountKind.AVAILABLE, AccountKind.FROZEN)
        ]
    )
    await session.flush()
    scopes = [f"tenant:{tenant.id}:available", "platform:funding"]
    accounts = await ledger._lock_accounts(session, scopes)
    await ledger._post_transfer(
        session,
        TransferKind.RECHARGE,
        f"old-payment:{tenant.id}",
        [(accounts[scopes[0]], 200_000), (accounts[scopes[1]], -200_000)],
    )
    order = await payments.create_payment(
        session, tenant.id, "100000", "new", mock_config, actor="test"
    )
    await payments.confirm_mock_payment(session, tenant.id, order.id, mock_config, actor="test")
    legacy = await session.scalar(
        select(FundLot).where(FundLot.external_ref == f"legacy:{tenant.id}")
    )
    assert legacy is not None and not legacy.refundable and legacy.credited_micro_cny == 200_000
    assert (
        await session.scalar(
            select(LedgerAccount.id).where(
                LedgerAccount.scope == f"tenant:{tenant.id}:refund_frozen"
            )
        )
        is not None
    )
    await payments.create_refund(
        session, tenant.id, order.id, "100000", "new funds", "full", mock_config, actor="test"
    )
    with pytest.raises(InsufficientBalanceError):
        await payments.create_refund(
            session, tenant.id, order.id, "10000", "old funds", "old", mock_config, actor="test"
        )
    assert legacy.available_micro_cny == 200_000
    await funds.assert_source_totals(session, tenant.id)
    await conserved(session, customer)
