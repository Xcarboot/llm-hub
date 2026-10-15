import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import admin
from app.core.config import settings
from app.core.errors import ConflictError
from app.db.models import (
    AccountKind,
    ApiKey,
    AuditEvent,
    GatewayRequest,
    LedgerAccount,
    LedgerEntry,
    LedgerTransfer,
    ModelCatalog,
    PriceVersion,
    ReservationStatus,
    Tenant,
    TransferKind,
)
from app.db.session import get_session
from app.main import app
from app.services import funds
from app.services.ledger import (
    create_tenant_accounts,
    ensure_platform_accounts,
    manual_recharge,
    reserve,
    reverse_transfer,
    settle,
)


@pytest.fixture
async def admin_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client
    app.dependency_overrides.clear()


def admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": settings.admin_token}


async def add_tenant_with_accounts(session: AsyncSession, name: str) -> Tenant:
    tenant = Tenant(name=name)
    session.add(tenant)
    await session.flush()
    await create_tenant_accounts(session, tenant.id)
    await ensure_platform_accounts(session)
    return tenant


async def add_price(session: AsyncSession, public_name: str = "enterprise-chat") -> PriceVersion:
    model = ModelCatalog(
        public_name=public_name,
        litellm_model=f"provider/{uuid4()}",
        route_order=0,
    )
    session.add(model)
    await session.flush()
    price = PriceVersion(
        model_id=model.id,
        input_micro_cny_per_million=100,
        output_micro_cny_per_million=200,
        effective_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(price)
    await session.flush()
    return price


async def add_request(
    session: AsyncSession,
    tenant: Tenant,
    *,
    model: str = "enterprise-chat",
    status: str = "COMPLETED",
    usage_json: object = None,
) -> GatewayRequest:
    request = GatewayRequest(
        tenant_id=tenant.id,
        model=model,
        stream=False,
        payload_hash=f"payload-{uuid4()}",
        status=status,
        usage_json=usage_json,
    )
    session.add(request)
    await session.flush()
    return request


@pytest.mark.asyncio
async def test_admin_session_requires_token_and_disables_cache(
    admin_client: AsyncClient,
) -> None:
    unauthorized = await admin_client.get("/admin/session")
    assert unauthorized.status_code == 401

    response = await admin_client.get("/admin/session", headers=admin_headers())
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("miss_precheck", [False, True])
async def test_duplicate_tenant_returns_conflict_without_bad_records(
    session: AsyncSession,
    admin_client: AsyncClient,
    miss_precheck: bool,
) -> None:
    created = await admin_client.post(
        "/admin/tenants", json={"name": "duplicate-tenant"}, headers=admin_headers()
    )
    assert created.status_code == 200
    # A missed precheck exercises the real unique error as in a concurrent insert.
    with patch.object(session, "rollback", wraps=session.rollback) as rollback:
        if miss_precheck:
            with patch.object(session, "scalar", new=AsyncMock(return_value=None)):
                duplicate = await admin_client.post(
                    "/admin/tenants",
                    json={"name": "duplicate-tenant"},
                    headers=admin_headers(),
                )
        else:
            duplicate = await admin_client.post(
                "/admin/tenants", json={"name": "duplicate-tenant"}, headers=admin_headers()
            )
        rollback.assert_awaited_once()
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "租户名称已存在"}
    assert duplicate.headers["cache-control"] == "no-store"
    for model, count in (
        (Tenant, 1),
        (LedgerAccount, 5),  # AVAILABLE, FROZEN, REFUND_FROZEN plus two platform accounts.
        (AuditEvent, 1),
        (LedgerTransfer, 0),
        (LedgerEntry, 0),
    ):
        assert await session.scalar(select(func.count()).select_from(model)) == count
    detail = await admin_client.get(
        f"/admin/tenants/{created.json()['id']}", headers=admin_headers()
    )
    assert detail.status_code == 200
    assert detail.json()["name"] == "duplicate-tenant"


@pytest.mark.asyncio
async def test_other_unique_failure_rolls_back_and_is_not_a_name_conflict(
    session: AsyncSession,
    admin_client: AsyncClient,
) -> None:
    await ensure_platform_accounts(session)
    await session.commit()

    async def duplicate_account(db: AsyncSession, tenant_id: UUID) -> None:
        db.add(
            LedgerAccount(tenant_id=tenant_id, kind=AccountKind.AVAILABLE, scope="platform:revenue")
        )
        await db.flush()

    with (
        patch.object(admin, "create_tenant_accounts", new=duplicate_account),
        patch.object(session, "rollback", wraps=session.rollback) as rollback,
        pytest.raises(IntegrityError, match="ledger_accounts.scope"),
    ):
        await admin_client.post(
            "/admin/tenants", json={"name": "failed-tenant"}, headers=admin_headers()
        )
    rollback.assert_awaited_once()
    assert await session.scalar(select(func.count(Tenant.id))) == 0
    assert await session.scalar(select(func.count(LedgerAccount.id))) == 2
    assert await session.scalar(select(func.count(AuditEvent.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error_class", [IntegrityError, OperationalError])
async def test_unrelated_commit_failure_is_not_swallowed_as_conflict(
    session: AsyncSession,
    admin_client: AsyncClient,
    error_class: type[IntegrityError] | type[OperationalError],
) -> None:
    failure = error_class("COMMIT", None, RuntimeError("database failure"))
    with (
        patch.object(session, "commit", new=AsyncMock(side_effect=failure)),
        pytest.raises(error_class) as caught,
    ):
        await admin_client.post(
            "/admin/tenants", json={"name": "commit-failure"}, headers=admin_headers()
        )
    assert caught.value is failure
    # get_session closes/rolls back unclassified non-integrity failures in production.
    await session.rollback()
    for model in (Tenant, LedgerAccount, AuditEvent):
        assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(
    ("sqlstate", "constraint", "expected"),
    [
        ("23505", "uq_tenants_name", True),
        ("23505", "uq_ledger_accounts_scope", False),
        ("23503", "uq_tenants_name", False),
        ("23505", None, False),
    ],
)
def test_postgres_name_conflict_requires_exact_constraint_and_unique_sqlstate(
    wrapped: bool,
    sqlstate: str,
    constraint: str | None,
    expected: bool,
) -> None:
    class DatabaseError(Exception):
        pass

    original = DatabaseError('duplicate key value violates unique constraint "uq_tenants_name"')
    original.sqlstate = sqlstate  # type: ignore[attr-defined]
    original.constraint_name = constraint  # type: ignore[attr-defined]
    if wrapped:
        dbapi_error = DatabaseError("wrapped asyncpg error")
        dbapi_error.__cause__ = original
    else:
        dbapi_error = original
    assert admin._is_tenant_name_conflict(IntegrityError(None, None, dbapi_error)) is expected


@pytest.mark.asyncio
async def test_overview_uses_settle_revenue_not_reservation_ceiling(
    session: AsyncSession,
    admin_client: AsyncClient,
) -> None:
    tenant = await add_tenant_with_accounts(session, "overview-tenant")
    price = await add_price(session)
    await manual_recharge(session, tenant.id, 1_000, "overview-funding")
    request = await add_request(
        session,
        tenant,
        usage_json={
            "prompt_tokens": 10,
            "completion_tokens": 5,
        },
    )
    await add_request(
        session,
        tenant,
        status="FAILED",
        usage_json={"prompt_tokens": "invalid", "completion_tokens": True},
    )
    reservation = await reserve(
        session,
        tenant.id,
        request.id,
        400,
        {"prompt": "must never be returned"},
        600,
    )
    await session.commit()

    reserved = await admin_client.get("/admin/overview?days=7", headers=admin_headers())
    assert reserved.status_code == 200
    assert reserved.json()["kpis"]["charged_micro_cny"] == 0

    await settle(session, reservation.id, 125, price.id)
    await session.commit()
    settled = await admin_client.get("/admin/overview?days=7", headers=admin_headers())
    body = settled.json()
    assert body["kpis"] == {
        "tenant_count": 1,
        "active_key_count": 0,
        "request_count": 2,
        "success_count": 1,
        "success_rate": 0.5,
        "charged_micro_cny": 125,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    assert len(body["trend"]) == 7
    assert body["trend"][-1]["charged_micro_cny"] == 125

    transfer = await session.scalar(
        select(LedgerTransfer).where(LedgerTransfer.kind == TransferKind.SETTLE)
    )
    assert transfer is not None
    original_entries = (
        await session.execute(
            select(LedgerEntry.id, LedgerEntry.amount_micro_cny).where(
                LedgerEntry.transfer_id == transfer.id
            )
        )
    ).all()
    await reverse_transfer(session, transfer.id, "overview-refund")
    await session.commit()
    reversed_response = await admin_client.get("/admin/overview?days=7", headers=admin_headers())
    assert reversed_response.status_code == 200
    reversed_body = reversed_response.json()
    assert reversed_body["kpis"] == {**body["kpis"], "charged_micro_cny": 0}
    assert reversed_body["trend"][-1]["charged_micro_cny"] == 0
    assert (
        await session.execute(
            select(LedgerEntry.id, LedgerEntry.amount_micro_cny).where(
                LedgerEntry.transfer_id == transfer.id
            )
        )
    ).all() == original_entries
    assert reservation.selected_price_version_id == price.id
    assert price.input_micro_cny_per_million == 100
    assert price.output_micro_cny_per_million == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("settled_days_ago", [1, 7])
async def test_overview_attributes_refund_to_its_transfer_day(
    session: AsyncSession,
    admin_client: AsyncClient,
    settled_days_ago: int,
) -> None:
    tenant = await add_tenant_with_accounts(session, "cross-day-refund")
    # A settlement reversal now requires a real funded, source-allocated reservation.
    # The former synthetic fixture produced negative FROZEN with no reservation.
    await manual_recharge(session, tenant.id, 125, "dated-funding")
    price = await add_price(session)
    gateway_request = await add_request(session, tenant)
    reservation = await reserve(session, tenant.id, gateway_request.id, 125, {}, 600)
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    revenue = await session.scalar(
        select(LedgerAccount).where(LedgerAccount.kind == AccountKind.REVENUE)
    )
    frozen = await session.scalar(
        select(LedgerAccount).where(
            LedgerAccount.tenant_id == tenant.id,
            LedgerAccount.kind == AccountKind.FROZEN,
        )
    )
    assert revenue is not None and frozen is not None
    # Dates are set on new fixtures before posting; historical postings are not updated.
    original = LedgerTransfer(
        kind=TransferKind.SETTLE,
        external_ref=f"reservation:{reservation.id}:settle",
        created_at=today - timedelta(days=settled_days_ago),
        posted=True,
    )
    session.add(original)
    await session.flush()
    session.add_all(
        [
            LedgerEntry(transfer_id=original.id, account_id=revenue.id, amount_micro_cny=125),
            LedgerEntry(transfer_id=original.id, account_id=frozen.id, amount_micro_cny=-125),
        ]
    )
    await session.flush()
    await funds.settle_allocation(session, tenant.id, reservation.id, 125)
    reservation.status = ReservationStatus.SETTLED
    reservation.selected_price_version_id = price.id
    await session.flush()
    await reverse_transfer(session, original.id, "cross-day-refund")
    await session.commit()

    response = await admin_client.get("/admin/overview?days=7", headers=admin_headers())
    assert response.status_code == 200
    body = response.json()
    amounts = {point["date"]: point["charged_micro_cny"] for point in body["trend"]}
    assert amounts[today.date().isoformat()] == -125
    if settled_days_ago == 1:
        assert amounts[(today - timedelta(days=1)).date().isoformat()] == 125
        assert body["kpis"]["charged_micro_cny"] == 0
    else:
        assert body["kpis"]["charged_micro_cny"] == -125
    today_only = await admin_client.get("/admin/overview?days=1", headers=admin_headers())
    assert today_only.json()["kpis"]["charged_micro_cny"] == -125
    assert today_only.json()["trend"][0]["charged_micro_cny"] == -125


@pytest.mark.asyncio
async def test_overview_excludes_non_settlement_and_unposted_revenue(
    session: AsyncSession,
    admin_client: AsyncClient,
) -> None:
    await ensure_platform_accounts(session)
    revenue = await session.scalar(
        select(LedgerAccount).where(LedgerAccount.kind == AccountKind.REVENUE)
    )
    funding = await session.scalar(
        select(LedgerAccount).where(LedgerAccount.kind == AccountKind.FUNDING)
    )
    assert revenue is not None and funding is not None
    for kind, posted in (
        (TransferKind.RECHARGE, True),
        (TransferKind.SETTLE, False),
        (TransferKind.REVERSAL, True),
    ):
        # Synthetic balanced fixtures challenge the query's kind/link/posted filters.
        transfer = LedgerTransfer(kind=kind, posted=posted, external_ref=f"excluded:{kind}")
        session.add(transfer)
        await session.flush()
        session.add_all(
            [
                LedgerEntry(transfer_id=transfer.id, account_id=revenue.id, amount_micro_cny=125),
                LedgerEntry(transfer_id=transfer.id, account_id=funding.id, amount_micro_cny=-125),
            ]
        )
        await session.flush()
        if kind == TransferKind.RECHARGE:
            # Generic recharge inversion is unsafe; only dedicated refund flows may refund it.
            with pytest.raises(ConflictError, match="only SETTLE"):
                await reverse_transfer(session, transfer.id, "non-settlement-refund")
    await session.commit()
    response = await admin_client.get("/admin/overview?days=1", headers=admin_headers())
    assert response.status_code == 200
    assert response.json()["kpis"]["charged_micro_cny"] == 0
    assert response.json()["trend"][0]["charged_micro_cny"] == 0


@pytest.mark.asyncio
async def test_tenant_aggregates_do_not_multiply_counts_or_balances(
    session: AsyncSession,
    admin_client: AsyncClient,
) -> None:
    tenant = await add_tenant_with_accounts(session, "aggregate-target")
    session.add_all(
        [
            ApiKey(tenant_id=tenant.id, prefix="gw_aggregate1", key_hash="a" * 64),
            ApiKey(tenant_id=tenant.id, prefix="gw_aggregate2", key_hash="b" * 64),
        ]
    )
    first_request = await add_request(session, tenant)
    await add_request(session, tenant, status="FAILED")
    await manual_recharge(session, tenant.id, 1_000, "aggregate-funding")
    await reserve(session, tenant.id, first_request.id, 100, {"safe": True}, 600)
    await session.commit()

    response = await admin_client.get(
        "/admin/tenants?search=aggregate&active=true", headers=admin_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["key_count"] == 2
    assert item["request_count"] == 2
    assert item["available_micro_cny"] == 900
    assert item["frozen_micro_cny"] == 100

    detail = await admin_client.get(f"/admin/tenants/{tenant.id}", headers=admin_headers())
    assert detail.json()["request_count"] == 2


@pytest.mark.asyncio
async def test_tenant_keyset_cursor_has_no_duplicates(
    session: AsyncSession,
    admin_client: AsyncClient,
) -> None:
    created_at = datetime.now(UTC) - timedelta(minutes=5)
    tenants = [Tenant(name=f"cursor-{index}", created_at=created_at) for index in range(3)]
    session.add_all(tenants)
    await session.commit()

    first = await admin_client.get("/admin/tenants?limit=2", headers=admin_headers())
    first_body = first.json()
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]
    second = await admin_client.get(
        "/admin/tenants",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
        headers=admin_headers(),
    )
    second_body = second.json()
    first_ids = {item["id"] for item in first_body["items"]}
    second_ids = {item["id"] for item in second_body["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 3
    assert second_body["has_more"] is False


@pytest.mark.asyncio
async def test_request_and_key_queries_redact_sensitive_fields_and_filter(
    session: AsyncSession,
    admin_client: AsyncClient,
) -> None:
    tenant = await add_tenant_with_accounts(session, "redaction-target")
    other = await add_tenant_with_accounts(session, "redaction-other")
    key_hash = "sensitive-key-hash" * 4
    session.add(ApiKey(tenant_id=tenant.id, prefix="gw_redacted1", key_hash=key_hash))
    target = await add_request(
        session,
        tenant,
        model="safe-model",
        status="COMPLETED",
        usage_json={
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "messages": [{"content": "commercial-secret-prompt"}],
        },
    )
    target.payload_hash = "sensitive-payload-hash"
    await add_request(session, tenant, model="different-model", status="FAILED")
    await add_request(session, other, model="safe-model", status="COMPLETED")
    await session.commit()

    response = await admin_client.get(
        "/admin/requests",
        params={
            "tenant_id": str(tenant.id),
            "status": "COMPLETED",
            "model": "safe-model",
        },
        headers=admin_headers(),
    )
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == str(target.id)
    assert body["items"][0]["usage_json"] == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
    }
    serialized = json.dumps(body)
    assert "payload_hash" not in serialized
    assert "commercial-secret-prompt" not in serialized
    assert "messages" not in serialized

    keys = await admin_client.get(f"/admin/tenants/{tenant.id}/keys", headers=admin_headers())
    key_body = keys.json()
    assert key_body["items"][0]["prefix"] == "gw_redacted1"
    assert "key_hash" not in json.dumps(key_body)
    assert key_hash not in json.dumps(key_body)


@pytest.mark.asyncio
async def test_models_ledger_and_reservation_filters_return_safe_shapes(
    session: AsyncSession,
    admin_client: AsyncClient,
) -> None:
    tenant = await add_tenant_with_accounts(session, "filter-target")
    price = await add_price(session, "filter-model")
    await manual_recharge(session, tenant.id, 1_000, "filter-funding")
    request = await add_request(session, tenant, model="filter-model")
    reservation = await reserve(
        session,
        tenant.id,
        request.id,
        300,
        {"messages": ["hidden"]},
        600,
    )
    await settle(session, reservation.id, 75, price.id)
    await session.commit()

    models = await admin_client.get("/admin/models", headers=admin_headers())
    model_items = models.json()["items"]
    assert model_items[0]["prices"][0]["id"] == str(price.id)

    transfers = await admin_client.get(
        "/admin/ledger/transfers",
        params={"tenant_id": str(tenant.id), "kind": "SETTLE"},
        headers=admin_headers(),
    )
    transfer_body = transfers.json()
    assert len(transfer_body["items"]) == 1
    assert transfer_body["items"][0]["transfer"]["kind"] == "SETTLE"
    assert sum(entry["amount_micro_cny"] for entry in transfer_body["items"][0]["entries"]) == 0

    reservations = await admin_client.get(
        "/admin/reservations",
        params={"tenant_id": str(tenant.id), "status": ReservationStatus.SETTLED.value},
        headers=admin_headers(),
    )
    reservation_body = reservations.json()
    assert len(reservation_body["items"]) == 1
    assert reservation_body["items"][0]["request_id"] == str(request.id)
    assert reservation_body["items"][0]["selected_price"]["id"] == str(price.id)
    assert "pricing_snapshot" not in json.dumps(reservation_body)


@pytest.mark.asyncio
async def test_admin_query_validation_and_not_found(admin_client: AsyncClient) -> None:
    headers = admin_headers()
    assert (await admin_client.get("/admin/overview?days=31", headers=headers)).status_code == 422
    assert (await admin_client.get("/admin/tenants?limit=101", headers=headers)).status_code == 422
    assert (
        await admin_client.get("/admin/tenants?cursor=not-base64", headers=headers)
    ).status_code == 422
    assert (
        await admin_client.get("/admin/ledger/transfers?kind=UNKNOWN", headers=headers)
    ).status_code == 422
    assert (
        await admin_client.get(
            "/admin/requests?from_at=2025-01-02T00:00:00&to_at=2025-01-01T00:00:00Z",
            headers=headers,
        )
    ).status_code == 422

    missing_id = uuid4()
    for path in (
        f"/admin/tenants/{missing_id}",
        f"/admin/tenants/{missing_id}/keys",
        f"/admin/tenants/{missing_id}/balance",
    ):
        assert (await admin_client.get(path, headers=headers)).status_code == 404
