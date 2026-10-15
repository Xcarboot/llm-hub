import asyncio
import os
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import Connection, create_engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.errors import ConflictError, InsufficientBalanceError
from app.db.base import Base
from app.db.models import (
    AccountKind,
    CustomerUser,
    FundAllocation,
    FundLot,
    GatewayRequest,
    LedgerAccount,
    LedgerEntry,
    LedgerTransfer,
    ModelCatalog,
    PaymentOrder,
    PriceVersion,
    Reservation,
    ReservationStatus,
    Tenant,
    TransferKind,
)
from app.schemas.commerce import AccountActivationCreate, PaymentCreate, ProviderWrite
from app.services import funds, ledger


async def tenant_accounts(session: AsyncSession) -> Tenant:
    tenant = Tenant(name=f"funds-{uuid4()}")
    session.add(tenant)
    await session.flush()
    await ledger.create_tenant_accounts(session, tenant.id)
    await ledger.ensure_platform_accounts(session)
    return tenant


async def request(session: AsyncSession, tenant_id: UUID) -> GatewayRequest:
    row = GatewayRequest(tenant_id=tenant_id, model="test", stream=False, payload_hash="a" * 64)
    session.add(row)
    await session.flush()
    return row


async def payment_credit(
    session: AsyncSession,
    tenant: Tenant,
    amount: int,
) -> tuple[PaymentOrder, FundLot]:
    await funds.lock_tenant(session, tenant.id)
    await funds.ensure_legacy_lot(session, tenant.id)
    order = PaymentOrder(
        tenant_id=tenant.id,
        mode="mock",
        amount_micro_cny=amount,
        out_trade_no=str(uuid4()),
        idempotency_key=str(uuid4()),
        payload_hash="a" * 64,
        status="PAID",
        paid_at=datetime.now(UTC),
    )
    session.add(order)
    await session.flush()
    scopes = [f"tenant:{tenant.id}:available", "platform:funding"]
    accounts = await ledger._lock_accounts(session, scopes)
    transfer = await ledger._post_transfer(
        session,
        TransferKind.RECHARGE,
        f"payment:{order.id}",
        [(accounts[scopes[0]], amount), (accounts[scopes[1]], -amount)],
    )
    order.credit_transfer_id = transfer.id
    lot = await funds.credit_lot(
        session,
        tenant.id,
        amount,
        f"payment:{order.id}",
        order.id,
        True,
    )
    await funds.assert_source_totals(session, tenant.id)
    return order, lot


async def refund_hold(
    session: AsyncSession,
    tenant: Tenant,
    order: PaymentOrder,
    amount: int,
) -> FundLot:
    await funds.lock_tenant(session, tenant.id)
    await funds.ensure_legacy_lot(session, tenant.id)
    lot = await funds.hold_refund(session, tenant, order, amount)
    await ledger.ensure_refund_account(session, tenant.id)
    scopes = [f"tenant:{tenant.id}:available", f"tenant:{tenant.id}:refund_frozen"]
    accounts = await ledger._lock_accounts(session, scopes)
    await ledger._post_transfer(
        session,
        TransferKind.REFUND_RESERVE,
        str(uuid4()),
        [(accounts[scopes[0]], -amount), (accounts[scopes[1]], amount)],
    )
    await funds.assert_source_totals(session, tenant.id)
    return lot


async def finish_refund(
    session: AsyncSession,
    tenant: Tenant,
    lot: FundLot,
    amount: int,
    *,
    success: bool,
) -> None:
    await funds.lock_tenant(session, tenant.id)
    await funds.finalize_refund(session, tenant.id, lot.id, amount, success=success)
    scopes = [
        f"tenant:{tenant.id}:refund_frozen",
        "platform:funding" if success else f"tenant:{tenant.id}:available",
    ]
    accounts = await ledger._lock_accounts(session, scopes)
    await ledger._post_transfer(
        session,
        TransferKind.REFUND if success else TransferKind.REFUND_RELEASE,
        str(uuid4()),
        [(accounts[scopes[0]], -amount), (accounts[scopes[1]], amount)],
    )
    await funds.assert_source_totals(session, tenant.id)


async def old_reservation(
    session: AsyncSession,
    tenant: Tenant,
    *,
    mismatched: bool = False,
    settled: bool = False,
) -> Reservation:
    """Seed OLD ledger-only history, deliberately bypassing new source hooks."""
    scopes = [
        f"tenant:{tenant.id}:available",
        f"tenant:{tenant.id}:frozen",
        "platform:funding",
        "platform:revenue",
    ]
    accounts = await ledger._lock_accounts(session, scopes)
    await ledger._post_transfer(
        session,
        TransferKind.RECHARGE,
        "recharge:old",
        [(accounts[scopes[0]], 1_000_000), (accounts[scopes[2]], -1_000_000)],
    )
    gateway = await request(session, tenant.id)
    row = Reservation(
        tenant_id=tenant.id,
        gateway_request_id=gateway.id,
        amount_micro_cny=200_000,
        pricing_snapshot={},
        status=ReservationStatus.RESERVED,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session.add(row)
    await session.flush()
    frozen = 190_000 if mismatched else 200_000
    await ledger._post_transfer(
        session,
        TransferKind.RESERVE,
        f"reservation:{row.id}:reserve",
        [(accounts[scopes[0]], -frozen), (accounts[scopes[1]], frozen)],
    )
    if settled:
        await ledger._post_transfer(
            session,
            TransferKind.SETTLE,
            f"reservation:{row.id}:settle",
            [
                (accounts[scopes[0]], 75_000),
                (accounts[scopes[1]], -200_000),
                (accounts[scopes[3]], 125_000),
            ],
        )
        row.status = ReservationStatus.SETTLED
        await session.flush()
    return row


async def test_legacy_source_is_nonrefundable_and_backfills_frozen(session: AsyncSession) -> None:
    tenant = await tenant_accounts(session)
    row = await old_reservation(session, tenant)
    lot = await funds.ensure_legacy_lot(session, tenant.id)
    assert lot is not None and not lot.refundable and lot.payment_order_id is None
    assert (lot.credited_micro_cny, lot.available_micro_cny, lot.call_frozen_micro_cny) == (
        1_000_000,
        800_000,
        200_000,
    )
    allocation = await session.scalar(
        select(FundAllocation).where(FundAllocation.reservation_id == row.id)
    )
    assert allocation is not None and allocation.amount_micro_cny == 200_000
    assert await funds.ensure_legacy_lot(session, tenant.id) is lot
    # An unrelated paid order cannot make legacy money refundable.
    order = PaymentOrder(
        tenant_id=tenant.id,
        mode="mock",
        amount_micro_cny=1_000_000,
        out_trade_no="uncredited",
        idempotency_key="uncredited",
        payload_hash="b" * 64,
        status="PAID",
    )
    session.add(order)
    await session.flush()
    with pytest.raises(ConflictError, match="not refundable"):
        await funds.hold_refund(session, tenant, order, 10_000)
    await ledger.release(session, row.id, "old-release")
    assert lot.available_micro_cny == 1_000_000 and lot.call_frozen_micro_cny == 0
    # Replaying an old recharge must not invent a second source.
    await ledger.manual_recharge(session, tenant.id, 1_000_000, "old")
    assert await session.scalar(select(func.count()).select_from(FundLot)) == 1
    await funds.assert_source_totals(session, tenant.id)


async def test_legacy_mismatch_stops_before_any_source_write(session: AsyncSession) -> None:
    tenant = await tenant_accounts(session)
    await old_reservation(session, tenant, mismatched=True)
    with pytest.raises(ConflictError, match="legacy frozen/reservation sum mismatch"):
        await funds.ensure_legacy_lot(session, tenant.id)
    assert await session.scalar(select(func.count()).select_from(FundLot)) == 0
    assert await session.scalar(select(func.count()).select_from(FundAllocation)) == 0


async def test_two_lots_fifo_consumption_and_refund_never_mix_sources(
    session: AsyncSession,
) -> None:
    tenant = await tenant_accounts(session)
    order1, lot1 = await payment_credit(session, tenant, 100_000)
    order2, lot2 = await payment_credit(session, tenant, 200_000)
    gateway = await request(session, tenant.id)
    row = await ledger.reserve(session, tenant.id, gateway.id, 150_000, {}, 600)
    assert (lot1.call_frozen_micro_cny, lot2.call_frozen_micro_cny) == (100_000, 50_000)
    await ledger.settle(session, row.id, 120_000, uuid4())
    assert (lot1.consumed_micro_cny, lot2.consumed_micro_cny) == (100_000, 20_000)
    assert (lot1.available_micro_cny, lot2.available_micro_cny) == (0, 180_000)
    with pytest.raises(InsufficientBalanceError, match="this order"):
        await funds.hold_refund(session, tenant, order1, 10_000)
    assert await refund_hold(session, tenant, order2, 180_000) is lot2
    await finish_refund(session, tenant, lot2, 180_000, success=True)
    assert lot2.refunded_micro_cny == 180_000 and lot2.consumed_micro_cny == 20_000
    assert lot1.refunded_micro_cny == 0
    with pytest.raises(ConflictError, match="hold/source mismatch"):
        await funds.finalize_refund(session, tenant.id, lot2.id, 180_000, success=True)


async def test_release_restores_each_lot_and_is_idempotent(session: AsyncSession) -> None:
    tenant = await tenant_accounts(session)
    _, lot1 = await payment_credit(session, tenant, 100_000)
    _, lot2 = await payment_credit(session, tenant, 200_000)
    gateway = await request(session, tenant.id)
    row = await ledger.reserve(session, tenant.id, gateway.id, 150_000, {}, 600)
    await ledger.release(session, row.id, "cancel")
    await ledger.release(session, row.id, "cancel-again")
    assert (lot1.available_micro_cny, lot2.available_micro_cny) == (100_000, 200_000)
    assert lot1.call_frozen_micro_cny == lot2.call_frozen_micro_cny == 0
    await funds.assert_source_totals(session, tenant.id)


async def test_recharge_and_payment_source_credit_are_idempotent(session: AsyncSession) -> None:
    tenant = await tenant_accounts(session)
    first = await ledger.manual_recharge(session, tenant.id, 100_000, "once")
    assert await ledger.manual_recharge(session, tenant.id, 100_000, "once") is first
    assert await session.scalar(select(func.count()).select_from(LedgerTransfer)) == 1
    assert await session.scalar(select(func.sum(FundLot.credited_micro_cny))) == 100_000
    with pytest.raises(ConflictError, match="different transfer fingerprint"):
        await ledger.manual_recharge(session, tenant.id, 200_000, "once")
    order, lot = await payment_credit(session, tenant, 100_000)
    assert (
        await funds.credit_lot(session, tenant.id, 100_000, f"payment:{order.id}", order.id, True)
        is lot
    )
    with pytest.raises(ConflictError, match="another source"):
        await funds.credit_lot(session, tenant.id, 100_000, "different", order.id, True)
    await funds.assert_source_totals(session, tenant.id)


async def test_refund_and_call_holds_cannot_overallocate(session: AsyncSession) -> None:
    tenant = await tenant_accounts(session)
    order, lot = await payment_credit(session, tenant, 100_000)
    await refund_hold(session, tenant, order, 60_000)
    gateway = await request(session, tenant.id)
    row = await ledger.reserve(session, tenant.id, gateway.id, 40_000, {}, 600)
    assert lot.available_micro_cny == 0
    with pytest.raises(InsufficientBalanceError):
        await funds.hold_refund(session, tenant, order, 10_000)
    # Roll back a failed reserve with a savepoint; helpers intentionally do not commit.
    async with session.begin_nested() as savepoint:
        with pytest.raises(InsufficientBalanceError):
            other = await request(session, tenant.id)
            await ledger.reserve(session, tenant.id, other.id, 1, {}, 600)
        await savepoint.rollback()
    await session.refresh(lot)
    await ledger.release(session, row.id, "release")
    await finish_refund(session, tenant, lot, 60_000, success=False)
    assert lot.available_micro_cny == 100_000
    assert lot.refund_frozen_micro_cny == lot.call_frozen_micro_cny == 0


@pytest.mark.parametrize("actual", [50_000, 150_000])
async def test_settlement_reversal_restores_available_not_frozen(
    session: AsyncSession,
    actual: int,
) -> None:
    tenant = await tenant_accounts(session)
    _, lot1 = await payment_credit(session, tenant, 100_000)
    _, lot2 = await payment_credit(session, tenant, 200_000)
    gateway = await request(session, tenant.id)
    row = await ledger.reserve(session, tenant.id, gateway.id, 200_000, {}, 600)
    await ledger.settle(session, row.id, actual, uuid4())
    transfer = await session.scalar(
        select(LedgerTransfer).where(LedgerTransfer.external_ref == f"reservation:{row.id}:settle")
    )
    assert transfer is not None
    original = (
        await session.execute(
            select(LedgerEntry.id, LedgerEntry.amount_micro_cny).where(
                LedgerEntry.transfer_id == transfer.id
            )
        )
    ).all()
    reversal = await ledger.reverse_transfer(session, transfer.id, "correct")
    assert await ledger.reverse_transfer(session, transfer.id, "correct") is reversal
    assert row.status == ReservationStatus.REVERSED
    assert (lot1.available_micro_cny, lot2.available_micro_cny) == (100_000, 200_000)
    assert lot1.consumed_micro_cny == lot2.consumed_micro_cny == 0
    accounts = await ledger._lock_accounts(
        session, [f"tenant:{tenant.id}:frozen", "platform:revenue"]
    )
    for account in accounts.values():
        assert await ledger.account_balance(session, account.id) == 0
    assert (
        await session.execute(
            select(LedgerEntry.id, LedgerEntry.amount_micro_cny).where(
                LedgerEntry.transfer_id == transfer.id
            )
        )
    ).all() == original
    with pytest.raises(ConflictError, match="already reversed"):
        await ledger.reverse_transfer(session, transfer.id, "second")
    with pytest.raises(ConflictError, match="only SETTLE"):
        await ledger.reverse_transfer(session, reversal.id, "reverse-reversal")
    await funds.assert_source_totals(session, tenant.id)


async def test_historical_settlement_reversal_uses_legacy_consumption(
    session: AsyncSession,
) -> None:
    tenant = await tenant_accounts(session)
    row = await old_reservation(session, tenant, settled=True)
    transfer = await session.scalar(
        select(LedgerTransfer).where(LedgerTransfer.external_ref == f"reservation:{row.id}:settle")
    )
    assert transfer is not None
    await ledger.reverse_transfer(session, transfer.id, "old-correction")
    lot = await session.scalar(select(FundLot).where(FundLot.tenant_id == tenant.id))
    assert lot is not None
    assert lot.available_micro_cny == lot.credited_micro_cny == 1_000_000
    assert lot.call_frozen_micro_cny == lot.consumed_micro_cny == 0
    assert not lot.refundable


@pytest.mark.parametrize(
    "kind",
    [
        TransferKind.RECHARGE,
        TransferKind.RESERVE,
        TransferKind.RELEASE,
        TransferKind.REFUND,
        TransferKind.REFUND_RESERVE,
        TransferKind.REFUND_RELEASE,
    ],
)
async def test_generic_reverse_rejects_nonsettlement(
    session: AsyncSession, kind: TransferKind
) -> None:
    transfer = LedgerTransfer(kind=kind, external_ref=f"unsafe:{kind}", posted=True)
    session.add(transfer)
    await session.flush()
    with pytest.raises(ConflictError, match="only SETTLE"):
        await ledger.reverse_transfer(session, transfer.id, "forbidden")


async def test_cross_tenant_and_fractional_refunds_are_rejected(session: AsyncSession) -> None:
    tenant = await tenant_accounts(session)
    other = await tenant_accounts(session)
    order, lot = await payment_credit(session, tenant, 100_000)
    with pytest.raises(ConflictError, match="tenant's paid order"):
        await funds.hold_refund(session, other, order, 10_000)
    with pytest.raises(ValueError, match="whole cents"):
        await funds.hold_refund(session, tenant, order, 1)
    assert lot.available_micro_cny == 100_000


async def test_source_check_constraints_and_email_normalization(session: AsyncSession) -> None:
    tenant = await tenant_accounts(session)
    user = CustomerUser(tenant_id=tenant.id, email=" Customer@Example.COM ")
    session.add(user)
    await session.flush()
    assert user.email == "customer@example.com" and user.password_hash is None and not user.active
    async with session.begin_nested() as savepoint:
        session.add(
            FundLot(
                tenant_id=tenant.id,
                external_ref="bad",
                credited_micro_cny=10,
                available_micro_cny=11,
                refundable=False,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await savepoint.rollback()


def test_commerce_dtos_keep_money_strings_and_never_output_provider_key() -> None:
    dto = PaymentCreate(amount_micro_cny="010000", idempotency_key="key")
    assert dto.model_dump()["amount_micro_cny"] == "10000"
    for invalid in ("1", "-10000", "1.0", "9223372036854775808", 10_000):
        with pytest.raises(ValidationError):
            PaymentCreate.model_validate({"amount_micro_cny": invalid, "idempotency_key": "key"})
    assert AccountActivationCreate(email=" A@Example.COM ").email == "a@example.com"
    provider = ProviderWrite(
        alias="test", model="m", api_base="https://example.com", api_key="secret"
    )
    assert "api_key" not in provider.model_dump()
    assert "secret" not in repr(provider)


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_commerce_settings_fail_closed(environment: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicitly hostile local configuration must not leak into independent checks.
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("CUSTOMER_COOKIE_SECURE", "false")
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", "inherited-local-key")
    with pytest.raises(RuntimeError, match="mock payments"):
        Settings(
            _env_file=None,
            app_env=environment,
            payment_mode="mock",
            admin_token="a" * 32,
            api_key_hmac_secret="b" * 32,
        ).assert_safe()
    with pytest.raises(RuntimeError, match="secure customer cookies"):
        Settings(
            _env_file=None,
            app_env=environment,
            payment_mode="disabled",
            customer_cookie_secure=False,
            admin_token="a" * 32,
            api_key_hmac_secret="b" * 32,
        ).assert_safe()
    settings = Settings(
        _env_file=None,
        app_env="test",
        payment_mode="mock",
        provider_encryption_key="",
        admin_token="a" * 32,
        api_key_hmac_secret="b" * 32,
    )
    assert settings.mock_payment_enabled
    settings.assert_safe()
    disabled = Settings(_env_file=None, app_env="test", payment_mode="disabled")
    assert not disabled.mock_payment_enabled
    assert settings.provider_encryption_key.get_secret_value() == ""


def migration_module(filename: str) -> Any:
    path = Path(__file__).resolve().parents[1] / "alembic/versions" / filename
    spec = spec_from_file_location(filename.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def apply_migration(connection: Connection, filename: str) -> None:
    context = MigrationContext.configure(connection, opts={"target_metadata": Base.metadata})
    with context.begin_transaction(), Operations.context(context):
        migration_module(filename).upgrade()


def test_financial_migration_downgrade_is_irreversible() -> None:
    with pytest.raises(RuntimeError, match="irreversible"):
        migration_module("0003_commerce_funds.py").downgrade()


def test_commerce_migration_matches_model_metadata() -> None:
    commerce = {
        "customer_users",
        "activation_tokens",
        "customer_sessions",
        "payment_orders",
        "fund_lots",
        "fund_allocations",
        "refund_requests",
        "provider_configs",
        "payment_operations",
    }
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        Base.metadata.create_all(
            connection,
            tables=[table for table in Base.metadata.sorted_tables if table.name not in commerce],
        )
        apply_migration(connection, "0003_commerce_funds.py")
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []
    engine.dispose()


def test_postgresql_migration_sql_is_append_only_and_enum_autocommitted() -> None:
    buffer = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": Base.metadata},
    )
    with context.begin_transaction(), Operations.context(context):
        migration_module("0003_commerce_funds.py").upgrade()
    sql = buffer.getvalue()
    assert sql.count("ALTER TYPE ") == 4
    assert sql.count("COMMIT;") >= 5
    assert sql.index("reconcile before migration") < sql.index("ALTER TYPE")
    assert sql.index("ALTER TYPE") < sql.index("CREATE TABLE customer_users")
    for forbidden in (
        "DROP ",
        "DELETE ",
        "UPDATE ",
        "INSERT ",
        "CREATE TRIGGER",
        "CREATE FUNCTION",
    ):
        assert forbidden not in sql


@pytest.mark.integration
@pytest.mark.parametrize("mismatched", [False, True])
async def test_postgresql_preserves_history_and_serializes_money(mismatched: bool) -> None:
    # MUST point to an explicitly supplied disposable PostgreSQL instance.
    # No .env/application database is consulted. Unique schemas are retained,
    # never dropped: this test performs no cleanup or destructive statements.
    url = os.environ.get("FUNDS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set FUNDS_TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = f"funds_test_{uuid4().hex}"
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(apply_migration, "0001_initial.py")
            await connection.run_sync(apply_migration, "0002_admin_query_indexes.py")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            tenant = Tenant(name="old-tenant")
            session.add(tenant)
            await session.flush()
            for kind in (AccountKind.AVAILABLE, AccountKind.FROZEN):
                session.add(
                    LedgerAccount(
                        tenant_id=tenant.id,
                        kind=kind,
                        scope=f"tenant:{tenant.id}:{kind.value.lower()}",
                    )
                )
            await ledger.ensure_platform_accounts(session)
            row = await old_reservation(session, tenant, mismatched=mismatched)
            await session.commit()
            old_entries = (
                await session.execute(
                    select(
                        LedgerEntry.id, LedgerEntry.account_id, LedgerEntry.amount_micro_cny
                    ).order_by(LedgerEntry.id)
                )
            ).all()
            tenant_id, reservation_id = tenant.id, row.id
        if mismatched:
            async with engine.connect() as connection:
                with pytest.raises(DBAPIError, match="reconcile before migration"):
                    await connection.run_sync(apply_migration, "0003_commerce_funds.py")
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT to_regclass('fund_lots')")) is None
                assert (
                    await connection.scalar(
                        text(
                            "SELECT COUNT(*) FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid "
                            "JOIN pg_namespace n ON n.oid=t.typnamespace "
                            "WHERE n.nspname=:schema AND e.enumlabel='REFUND_FROZEN'"
                        ),
                        {"schema": schema},
                    )
                    == 0
                )
            return
        async with engine.connect() as connection:
            await connection.run_sync(apply_migration, "0003_commerce_funds.py")
        async with maker() as session:
            assert (
                await session.execute(
                    select(
                        LedgerEntry.id, LedgerEntry.account_id, LedgerEntry.amount_micro_cny
                    ).order_by(LedgerEntry.id)
                )
            ).all() == old_entries
            assert await session.scalar(select(func.count()).select_from(FundLot)) == 0
            lot = await funds.ensure_legacy_lot(session, tenant_id)
            assert lot is not None and not lot.refundable
            await ledger.release(session, reservation_id, "migration-release")
            assert lot.available_micro_cny == 1_000_000
            await session.commit()
            # Immutable historical entries remain protected by their original trigger.
            async with session.begin_nested() as savepoint:
                with pytest.raises(DBAPIError, match="immutable"):
                    await session.execute(
                        text("UPDATE ledger_entries SET amount_micro_cny = amount_micro_cny")
                    )
                await savepoint.rollback()
            fresh = await tenant_accounts(session)
            _, lot1 = await payment_credit(session, fresh, 100_000)
            _, lot2 = await payment_credit(session, fresh, 200_000)
            gateway = await request(session, fresh.id)
            reserved = await ledger.reserve(session, fresh.id, gateway.id, 200_000, {}, 600)
            model = ModelCatalog(public_name="pg-test", litellm_model="pg-test", route_order=0)
            session.add(model)
            await session.flush()
            price = PriceVersion(
                model_id=model.id,
                input_micro_cny_per_million=1,
                output_micro_cny_per_million=1,
                effective_at=datetime.now(UTC),
            )
            session.add(price)
            await session.flush()
            await ledger.settle(session, reserved.id, 150_000, price.id)
            settled = await session.scalar(
                select(LedgerTransfer).where(
                    LedgerTransfer.external_ref == f"reservation:{reserved.id}:settle"
                )
            )
            assert settled is not None
            await ledger.reverse_transfer(session, settled.id, "pg-correction")
            assert (lot1.available_micro_cny, lot2.available_micro_cny) == (100_000, 200_000)
            await funds.assert_source_totals(session, fresh.id)
            concurrent_tenant = await tenant_accounts(session)
            concurrent_order, _ = await payment_credit(session, concurrent_tenant, 100_000)
            concurrent_tenant_id, concurrent_order_id = concurrent_tenant.id, concurrent_order.id
            await session.commit()

        async def competing_hold(refund: bool) -> str:
            async with maker() as session:
                try:
                    async with session.begin():
                        if refund:
                            tenant = await session.get(Tenant, concurrent_tenant_id)
                            order = await session.get(PaymentOrder, concurrent_order_id)
                            assert tenant is not None and order is not None
                            await refund_hold(session, tenant, order, 80_000)
                        else:
                            gateway = await request(session, concurrent_tenant_id)
                            await ledger.reserve(
                                session, concurrent_tenant_id, gateway.id, 80_000, {}, 600
                            )
                    return "held"
                except InsufficientBalanceError:
                    return "insufficient"

        outcomes = await asyncio.gather(competing_hold(True), competing_hold(False))
        assert sorted(outcomes) == ["held", "insufficient"]
        async with maker() as session:
            await funds.assert_source_totals(session, concurrent_tenant_id)
            call_tenant = await tenant_accounts(session)
            await payment_credit(session, call_tenant, 100_000)
            call_tenant_id = call_tenant.id
            await session.commit()
        # Both inserts take tenant FK KEY SHARE before calling reserve. A plain
        # FOR UPDATE tenant lock would deadlock on this forced lock-upgrade race.
        barrier = asyncio.Barrier(2)

        async def concurrent_call() -> str:
            async with maker() as session:
                try:
                    async with session.begin():
                        gateway = await request(session, call_tenant_id)
                        await barrier.wait()
                        await ledger.reserve(session, call_tenant_id, gateway.id, 80_000, {}, 600)
                    return "held"
                except InsufficientBalanceError:
                    return "insufficient"

        outcomes = await asyncio.wait_for(
            asyncio.gather(concurrent_call(), concurrent_call()),
            timeout=10,
        )
        assert sorted(outcomes) == ["held", "insufficient"]
        async with maker() as session:
            await funds.assert_source_totals(session, call_tenant_id)
    finally:
        await engine.dispose()
