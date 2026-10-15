"""Transaction-owned postings, tested in retained isolated PostgreSQL schemas.

PORTAL_TEST_DATABASE_URL must explicitly identify a disposable test PostgreSQL
instance (postgresql+asyncpg://...). Never use the application/.env database.
Like test_funds, each test creates its own UUID schema and retains it: no database,
volume, schema or financial row is deleted. Without that opt-in, PG tests skip.
"""

import os
from collections.abc import AsyncIterator
from io import StringIO
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import FundLot, LedgerAccount, LedgerEntry, LedgerTransfer, TransferKind
from app.services import funds, ledger
from tests.test_funds import (
    apply_migration,
    finish_refund,
    migration_module,
    payment_credit,
    refund_hold,
    tenant_accounts,
)

MIGRATION = "0004_sealed_ledger.py"


def migration_sql(direction: str, dialect: str = "postgresql") -> str:
    buffer = StringIO()
    context = MigrationContext.configure(
        dialect_name=dialect, opts={"as_sql": True, "output_buffer": buffer}
    )
    with context.begin_transaction(), Operations.context(context):
        getattr(migration_module(MIGRATION), direction)()
    return buffer.getvalue()


def test_sealing_migration_only_adds_transaction_metadata_and_insert_guard() -> None:
    migration = migration_module(MIGRATION)
    assert migration.revision == "0004" and migration.down_revision == "0003"
    sql = migration_sql("upgrade")
    assert "posting_transaction_id BIGINT DEFAULT txid_current() NOT NULL" in sql
    assert "BEFORE INSERT ON ledger_entries" in sql
    assert "parent_transaction_id IS DISTINCT FROM txid_current()" in sql
    assert "FROM ledger_transfers WHERE id = NEW.transfer_id" in sql
    assert "RETURN NEW" in sql
    for forbidden in ("UPDATE ", "DELETE ", "INSERT INTO", "DROP ", "amount_micro_cny"):
        assert forbidden not in sql
    assert "immutable" not in sql and "balanced" not in sql
    assert "posting_transaction_id" not in LedgerTransfer.__table__.columns


def test_sealing_downgrade_only_removes_its_own_protection() -> None:
    sql = migration_sql("downgrade")
    assert "DROP TRIGGER ledger_entries_posting_transaction ON ledger_entries" in sql
    assert "DROP FUNCTION assert_ledger_posting_transaction()" in sql
    assert "ALTER TABLE ledger_transfers DROP COLUMN posting_transaction_id" in sql
    for forbidden in ("DROP TABLE", "UPDATE ", "DELETE ", "immutable", "balanced", "CASCADE"):
        assert forbidden not in sql


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_sealing_is_postgresql_only(direction: str) -> None:
    assert migration_sql(direction, "sqlite") == ""


@pytest.fixture
async def commerce_pg() -> AsyncIterator[AsyncEngine]:
    url = os.environ.get("PORTAL_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "set PORTAL_TEST_DATABASE_URL to an isolated disposable PostgreSQL database; "
            "application/production databases are never used"
        )
    schema = f"portal_sealing_test_{uuid4().hex}"
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(apply_migration, "0001_initial.py")
            await connection.run_sync(apply_migration, "0002_admin_query_indexes.py")
        # 0003 extends enums in autocommit blocks; use its existing migration helper.
        async with engine.connect() as connection:
            await connection.run_sync(apply_migration, "0003_commerce_funds.py")
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def sealed_pg(commerce_pg: AsyncEngine) -> AsyncEngine:
    async with commerce_pg.begin() as connection:
        await connection.run_sync(apply_migration, MIGRATION)
    return commerce_pg


async def financial_snapshot(session: AsyncSession) -> dict[str, list[tuple[Any, ...]]]:
    # ORM table columns intentionally exclude the DB-only sealing metadata.
    result = {}
    for model in (LedgerTransfer, LedgerEntry, FundLot):
        rows = (await session.execute(select(model.__table__).order_by(model.id))).all()
        result[model.__tablename__] = [tuple(row) for row in rows]
    rows = (
        await session.execute(
            select(
                LedgerAccount.id,
                func.coalesce(func.sum(LedgerEntry.amount_micro_cny), 0),
            )
            .outerjoin(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
            .group_by(LedgerAccount.id)
            .order_by(LedgerAccount.id)
        )
    ).all()
    result["balances"] = [tuple(row) for row in rows]
    return result


async def reject_balanced_append(
    engine: AsyncEngine, transfer_id: UUID, account_ids: tuple[UUID, UUID]
) -> None:
    assert account_ids[0] != account_ids[1]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        before = await financial_snapshot(session)
        occupied = set(
            await session.scalars(
                select(LedgerEntry.account_id).where(LedgerEntry.transfer_id == transfer_id)
            )
        )
        # Avoid the old per-transfer/account unique constraint: this is the actual
        # loophole, two NEW accounts and a net-zero pair in one INSERT statement.
        assert occupied.isdisjoint(account_ids)
        await session.rollback()
        with pytest.raises(DBAPIError, match="sealed to its posting transaction"):
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO ledger_entries "
                        "(id, transfer_id, account_id, amount_micro_cny) VALUES "
                        "(:first_id, :transfer, :first_account, 10000), "
                        "(:second_id, :transfer, :second_account, -10000)"
                    ),
                    {
                        "first_id": uuid4(),
                        "second_id": uuid4(),
                        "transfer": transfer_id,
                        "first_account": account_ids[0],
                        "second_account": account_ids[1],
                    },
                )
        assert await financial_snapshot(session) == before


@pytest.mark.integration
async def test_migration_backfills_and_seals_history_without_changing_money(
    commerce_pg: AsyncEngine,
) -> None:
    maker = async_sessionmaker(commerce_pg, expire_on_commit=False)
    async with maker() as session:
        tenant = await tenant_accounts(session)
        transfer = await ledger.manual_recharge(session, tenant.id, 100_000, "historical")
        other = await tenant_accounts(session)
        await ledger.manual_recharge(session, other.id, 200_000, "other-history")
        await session.commit()
        before = await financial_snapshot(session)
        accounts = await ledger._lock_accounts(
            session, [f"tenant:{tenant.id}:frozen", "platform:revenue"]
        )
        unused = (accounts[f"tenant:{tenant.id}:frozen"].id, accounts["platform:revenue"].id)
        transfer_id = transfer.id

    async with commerce_pg.begin() as connection:
        migration_txid = await connection.scalar(text("SELECT txid_current()"))
        await connection.run_sync(apply_migration, MIGRATION)
        owners = (
            (await connection.execute(text("SELECT posting_transaction_id FROM ledger_transfers")))
            .scalars()
            .all()
        )
        assert len(owners) == 2 and set(owners) == {migration_txid}
    async with maker() as session:
        assert await financial_snapshot(session) == before
        assert await session.scalar(text("SELECT txid_current()")) != migration_txid
        for statement in (
            "UPDATE ledger_entries SET amount_micro_cny = amount_micro_cny",
            "UPDATE ledger_transfers SET posting_transaction_id = txid_current()",
        ):
            with pytest.raises(DBAPIError, match="immutable"):
                async with session.begin_nested():
                    await session.execute(text(statement))
        assert await financial_snapshot(session) == before
    await reject_balanced_append(commerce_pg, transfer_id, unused)

    # Downgrade must preserve both the financial snapshot and all 0001 triggers.
    async with commerce_pg.begin() as connection:

        def downgrade(sync_connection: Any) -> None:
            context = MigrationContext.configure(sync_connection)
            with Operations.context(context):
                migration_module(MIGRATION).downgrade()

        await connection.run_sync(downgrade)
        assert (
            await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'ledger_transfers' "
                    "AND column_name = 'posting_transaction_id'"
                )
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT to_regprocedure('assert_ledger_posting_transaction()')")
            )
            is None
        )
        triggers = set(
            (
                await connection.execute(
                    text(
                        "SELECT t.tgname FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = current_schema() AND NOT t.tgisinternal"
                    )
                )
            ).scalars()
        )
        assert triggers == {
            "ledger_entries_immutable",
            "ledger_transfers_immutable",
            "price_versions_immutable",
            "ledger_transfer_balanced",
            "new_ledger_transfer_balanced",
        }
    async with maker() as session:
        assert await financial_snapshot(session) == before


@pytest.mark.integration
async def test_new_transfer_allows_multiple_flushes_and_savepoints_then_seals(
    sealed_pg: AsyncEngine,
) -> None:
    maker = async_sessionmaker(sealed_pg, expire_on_commit=False)
    async with maker() as session:
        tenant = await tenant_accounts(session)
        scopes = [f"tenant:{tenant.id}:available", f"tenant:{tenant.id}:frozen"]
        accounts = await ledger._lock_accounts(
            session, scopes + ["platform:funding", "platform:revenue"]
        )
        transfer = LedgerTransfer(
            kind=TransferKind.RECHARGE, external_ref="multi-flush", posted=True
        )
        session.add(transfer)
        await session.flush()  # Parent first; unmapped DB default must fill its owner.
        owner = await session.scalar(
            text("SELECT posting_transaction_id FROM ledger_transfers WHERE id = :id"),
            {"id": transfer.id},
        )
        assert owner == await session.scalar(text("SELECT txid_current()"))
        session.add(
            LedgerEntry(
                transfer_id=transfer.id, account_id=accounts[scopes[0]].id, amount_micro_cny=10_000
            )
        )
        await session.flush()  # An unbalanced intermediate state is allowed before commit.
        async with session.begin_nested():
            assert owner == await session.scalar(text("SELECT txid_current()"))
            session.add(
                LedgerEntry(
                    transfer_id=transfer.id,
                    account_id=accounts[scopes[1]].id,
                    amount_micro_cny=-10_000,
                )
            )
            await session.flush()
        await session.commit()
        transfer_id = transfer.id
        unused = (accounts["platform:funding"].id, accounts["platform:revenue"].id)
    await reject_balanced_append(sealed_pg, transfer_id, unused)


@pytest.mark.integration
async def test_post_transfer_recharge_and_refund_can_commit(sealed_pg: AsyncEngine) -> None:
    maker = async_sessionmaker(sealed_pg, expire_on_commit=False)
    async with maker() as session:
        tenant = await tenant_accounts(session)
        original = await ledger.manual_recharge(session, tenant.id, 100_000, "normal-recharge")
        await session.commit()
        assert (
            await ledger.manual_recharge(session, tenant.id, 100_000, "normal-recharge") is original
        )
        order, lot = await payment_credit(session, tenant, 200_000)
        await session.commit()
        await refund_hold(session, tenant, order, 100_000)
        await session.commit()
        await finish_refund(session, tenant, lot, 100_000, success=True)
        await session.commit()
        await funds.assert_source_totals(session, tenant.id)
        assert (lot.available_micro_cny, lot.refunded_micro_cny, lot.refund_frozen_micro_cny) == (
            100_000,
            100_000,
            0,
        )
        kinds = list(await session.scalars(select(LedgerTransfer.kind)))
        assert kinds.count(TransferKind.RECHARGE) == 2
        assert kinds.count(TransferKind.REFUND_RESERVE) == kinds.count(TransferKind.REFUND) == 1
        accounts = await ledger._lock_accounts(
            session,
            [f"tenant:{tenant.id}:available", f"tenant:{tenant.id}:frozen", "platform:revenue"],
        )
        assert (
            await ledger.account_balance(session, accounts[f"tenant:{tenant.id}:available"].id)
            == 200_000
        )
        unused = (accounts[f"tenant:{tenant.id}:frozen"].id, accounts["platform:revenue"].id)
        transfer_id = original.id
    await reject_balanced_append(sealed_pg, transfer_id, unused)


@pytest.mark.integration
@pytest.mark.parametrize("entry_count", [0, 1])
async def test_original_deferred_balance_triggers_still_reject_bad_transfers(
    sealed_pg: AsyncEngine, entry_count: int
) -> None:
    maker = async_sessionmaker(sealed_pg, expire_on_commit=False)
    async with maker() as session:
        tenant = await tenant_accounts(session)
        account = await session.scalar(
            select(LedgerAccount).where(LedgerAccount.scope == f"tenant:{tenant.id}:available")
        )
        assert account is not None
        account_id = account.id
        await session.commit()
        before = await financial_snapshot(session)
        await session.rollback()
        with pytest.raises(DBAPIError, match="not balanced"):
            async with session.begin():
                transfer = LedgerTransfer(
                    kind=TransferKind.RECHARGE, external_ref="invalid", posted=True
                )
                session.add(transfer)
                await session.flush()
                if entry_count:
                    session.add(
                        LedgerEntry(
                            transfer_id=transfer.id, account_id=account_id, amount_micro_cny=10_000
                        )
                    )
                    await session.flush()
        assert await financial_snapshot(session) == before
