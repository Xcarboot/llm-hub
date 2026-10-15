from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
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
    TransferKind,
)
from app.services.ledger import create_tenant_accounts, ensure_platform_accounts, manual_recharge
from tests import test_customer_auth as auth_tests
from tests.test_customer_auth import activated_customer, add_customer_tenant

portal_client = auth_tests.portal_client


async def test_keys_scoped_paginated_once_only_and_immediate_revoke(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    tenant, headers = await activated_customer(session, portal_client)
    other = await add_customer_tenant(session, "other")
    other_key = ApiKey(tenant_id=other.id, prefix="gw_otherkey", key_hash="a" * 64)
    session.add(other_key)
    await session.commit()
    created = []
    for _ in range(3):
        response = await portal_client.post("/portal/keys", headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        created.append(response.json())
    page = await portal_client.get("/portal/keys", params={"limit": 2, "tenant_id": str(other.id)})
    assert len(page.json()["items"]) == 2 and page.json()["has_more"]
    second = await portal_client.get(
        "/portal/keys", params={"limit": 2, "cursor": page.json()["next_cursor"]}
    )
    assert len(second.json()["items"]) == 1 and not second.json()["has_more"]
    listed = page.json()["items"] + second.json()["items"]
    assert {item["id"] for item in listed} == {item["id"] for item in created}
    for key in listed:
        assert set(key) == {"id", "prefix", "revoked", "created_at"}
    assert "key_hash" not in page.text and "api_key" not in page.text
    assert created[0]["api_key"] not in page.text
    foreign = await portal_client.get(f"/portal/keys/{other_key.id}")
    assert foreign.status_code == 404
    foreign = await portal_client.post(f"/portal/keys/{other_key.id}/revoke", headers=headers)
    assert foreign.status_code == 404 and not other_key.revoked
    key = created[0]
    before = await portal_client.get(
        "/v1/models", headers={"Authorization": "Bearer " + key["api_key"]}
    )
    assert before.status_code == 200
    for _ in range(2):
        response = await portal_client.post(f"/portal/keys/{key['id']}/revoke", headers=headers)
        assert response.status_code == 200 and response.json()["revoked"]
        assert response.headers["cache-control"] == "no-store"
    after = await portal_client.get(
        "/v1/models", headers={"Authorization": "Bearer " + key["api_key"]}
    )
    assert after.status_code == 401
    assert (
        await session.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "api_key.revoke")
        )
        == 1
    )
    assert (await session.get(ApiKey, UUID(key["id"]))).tenant_id == tenant.id  # type: ignore[union-attr]


async def test_admin_revoke_requires_admin_and_tenant_match(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    tenant, headers = await activated_customer(session, portal_client)
    other = await add_customer_tenant(session, "other")
    response = await portal_client.post("/portal/keys", headers=headers)
    key_id = response.json()["id"]
    app = portal_client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides.pop(require_admin)
    assert (
        await portal_client.post(f"/admin/tenants/{tenant.id}/keys/{key_id}/revoke")
    ).status_code == 401
    app.dependency_overrides[require_admin] = lambda: None
    assert (
        await portal_client.post(f"/admin/tenants/{other.id}/keys/{key_id}/revoke")
    ).status_code == 404
    result = await portal_client.post(f"/admin/tenants/{tenant.id}/keys/{key_id}/revoke")
    assert result.status_code == 200 and result.json()["revoked"]


async def test_balance_and_ledger_entries_do_not_leak_other_tenants(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    tenant, _ = await activated_customer(session, portal_client)
    other = await add_customer_tenant(session, "other")
    await create_tenant_accounts(session, tenant.id)
    await create_tenant_accounts(session, other.id)
    await ensure_platform_accounts(session)
    await manual_recharge(session, tenant.id, 1234567890123, "own-recharge")
    await manual_recharge(session, other.id, 500000, "other-recharge")
    own_account = await session.scalar(
        select(LedgerAccount).where(
            LedgerAccount.tenant_id == tenant.id, LedgerAccount.kind == AccountKind.AVAILABLE
        )
    )
    other_account = await session.scalar(
        select(LedgerAccount).where(
            LedgerAccount.tenant_id == other.id, LedgerAccount.kind == AccountKind.AVAILABLE
        )
    )
    assert own_account and other_account
    cross = LedgerTransfer(kind=TransferKind.RECHARGE, external_ref="cross-tenant", posted=True)
    session.add(cross)
    await session.flush()
    session.add_all(
        [
            LedgerEntry(transfer_id=cross.id, account_id=own_account.id, amount_micro_cny=1),
            LedgerEntry(transfer_id=cross.id, account_id=other_account.id, amount_micro_cny=-1),
        ]
    )
    await session.commit()
    response = await portal_client.get("/portal/balance", params={"tenant_id": str(other.id)})
    assert response.json() == {
        "available_micro_cny": "1234567890124",
        "frozen_micro_cny": "0",
        "refund_frozen_micro_cny": "0",
        "total_micro_cny": "1234567890124",
    }
    response = await portal_client.get(
        "/portal/ledger/transfers", params={"tenant_id": str(other.id)}
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert "other-recharge" not in response.text and str(other.id) not in response.text
    for item in response.json()["items"]:
        for entry in item["entries"]:
            assert str(tenant.id) in entry["account_scope"]
    assert response.headers["cache-control"] == "no-store"


async def test_requests_own_filters_safe_usage_and_model_prices(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    tenant, _ = await activated_customer(session, portal_client)
    other = await add_customer_tenant(session, "other")
    for owner in (tenant, other):
        for status in ("COMPLETED", "FAILED"):
            session.add(
                GatewayRequest(
                    tenant_id=owner.id,
                    model="public-chat",
                    stream=False,
                    payload_hash="a" * 64,
                    status=status,
                    usage_json={
                        "prompt_tokens": 4,
                        "completion_tokens": 6,
                        "prompt": "never expose",
                        "api_key": "never expose",
                    },
                )
            )
    model = ModelCatalog(public_name="public-chat", litellm_model="provider/chat", route_order=0)
    session.add(model)
    await session.flush()
    session.add(
        PriceVersion(
            model_id=model.id,
            input_micro_cny_per_million=100,
            output_micro_cny_per_million=200,
            effective_at=datetime.now(UTC),
        )
    )
    await session.commit()
    response = await portal_client.get(
        "/portal/requests", params={"tenant_id": str(other.id), "limit": 1}
    )
    assert response.json()["has_more"]
    second = await portal_client.get(
        "/portal/requests", params={"cursor": response.json()["next_cursor"]}
    )
    assert len(second.json()["items"]) == 1
    assert str(other.id) not in response.text and "never expose" not in response.text
    assert "payload_hash" not in response.text and "api_key" not in response.text
    filtered = await portal_client.get(
        "/portal/requests", params={"model": "public-chat", "status": "FAILED"}
    )
    assert len(filtered.json()["items"]) == 1
    assert filtered.json()["items"][0]["status"] == "FAILED"
    response = await portal_client.get("/portal/models")
    assert response.status_code == 200
    assert response.json()["items"][0]["prices"][0]["input_micro_cny_per_million"] == 100
    assert response.headers["cache-control"] == "no-store"


async def test_balance_includes_both_holds_and_ignores_unposted_entries(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    tenant, _ = await activated_customer(session, portal_client)
    await create_tenant_accounts(session, tenant.id)
    await ensure_platform_accounts(session)
    await manual_recharge(session, tenant.id, 1000, "recharge-holds")
    accounts = {
        account.kind: account
        for account in (
            await session.scalars(select(LedgerAccount).where(LedgerAccount.tenant_id == tenant.id))
        ).all()
    }
    hold = LedgerTransfer(kind=TransferKind.RESERVE, external_ref="holds", posted=True)
    pending = LedgerTransfer(kind=TransferKind.RELEASE, external_ref="unposted", posted=False)
    session.add_all([hold, pending])
    await session.flush()
    for transfer, changes in (
        (
            hold,
            {AccountKind.AVAILABLE: -300, AccountKind.FROZEN: 100, AccountKind.REFUND_FROZEN: 200},
        ),
        (pending, {AccountKind.AVAILABLE: 100, AccountKind.FROZEN: -100}),
    ):
        for kind, amount in changes.items():
            session.add(
                LedgerEntry(
                    transfer_id=transfer.id, account_id=accounts[kind].id, amount_micro_cny=amount
                )
            )
    await session.commit()
    response = await portal_client.get("/portal/balance")
    assert response.json() == {
        "available_micro_cny": "700",
        "frozen_micro_cny": "100",
        "refund_frozen_micro_cny": "200",
        "total_micro_cny": "1000",
    }
    first = (await portal_client.get("/portal/ledger/transfers", params={"limit": 1})).json()
    assert first["has_more"]
    second = (
        await portal_client.get(
            "/portal/ledger/transfers",
            params={
                "cursor": first["next_cursor"],
                "limit": 1,
            },
        )
    ).json()
    assert len(second["items"]) == 1 and not second["has_more"]
    assert first["items"][0]["transfer"]["id"] != second["items"][0]["transfer"]["id"]


@pytest.mark.parametrize("path", ["keys", "requests", "ledger/transfers", "models"])
async def test_pagination_validation(
    session: AsyncSession, portal_client: AsyncClient, path: str
) -> None:
    await activated_customer(session, portal_client)
    for params in ({"limit": 101}, {"limit": 0}, {"cursor": "not-valid"}):
        response = await portal_client.get("/portal/" + path, params=params)
        assert response.status_code == 422


async def test_key_write_requires_origin_and_csrf(
    session: AsyncSession, portal_client: AsyncClient
) -> None:
    _, headers = await activated_customer(session, portal_client)
    for supplied in (
        {},
        {"X-CSRF-Token": headers["X-CSRF-Token"]},
        {**headers, "Origin": "https://evil.example"},
    ):
        assert (await portal_client.post("/portal/keys", headers=supplied)).status_code == 403
    assert await session.scalar(select(func.count(ApiKey.id))) == 0
