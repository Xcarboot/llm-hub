from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.db.models import (
    LedgerAccount,
    LedgerEntry,
    LedgerTransfer,
    ReservationStatus,
    Tenant,
)
from app.services.ledger import (
    account_balance,
    create_tenant_accounts,
    ensure_platform_accounts,
    manual_recharge,
    release,
    reserve,
    settle,
)


async def seed_ledger(session: AsyncSession) -> Tenant:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    await create_tenant_accounts(session, tenant.id)
    await ensure_platform_accounts(session)
    await manual_recharge(session, tenant.id, 1_000_000, str(uuid4()))
    await session.commit()
    return tenant


@pytest.mark.asyncio
async def test_every_transfer_is_balanced(session: AsyncSession) -> None:
    tenant = await seed_ledger(session)
    reservation = await reserve(session, tenant.id, uuid4(), 200_000, {"bound": True}, 600)
    await settle(session, reservation.id, 125_000, uuid4())
    await session.commit()
    sums = (
        await session.execute(
            select(LedgerEntry.transfer_id, func.sum(LedgerEntry.amount_micro_cny))
            .group_by(LedgerEntry.transfer_id)
        )
    ).all()
    assert sums
    assert all(total == 0 for _, total in sums)
    assert len((await session.scalars(select(LedgerTransfer))).all()) == 3


@pytest.mark.asyncio
async def test_reserve_settle_refunds_difference(session: AsyncSession) -> None:
    tenant = await seed_ledger(session)
    reservation = await reserve(session, tenant.id, uuid4(), 200_000, {"bound": True}, 600)
    await session.commit()
    available = await session.scalar(
        select(LedgerAccount).where(
            LedgerAccount.scope == f"tenant:{tenant.id}:available"
        )
    )
    assert available is not None
    assert await account_balance(session, available.id) == 800_000
    await settle(session, reservation.id, 125_000, uuid4())
    await session.commit()
    assert reservation.status == ReservationStatus.SETTLED
    assert await account_balance(session, available.id) == 875_000


@pytest.mark.asyncio
async def test_release_returns_full_reservation(session: AsyncSession) -> None:
    tenant = await seed_ledger(session)
    reservation = await reserve(session, tenant.id, uuid4(), 200_000, {"bound": True}, 600)
    await release(session, reservation.id, "operator-release")
    await session.commit()
    assert reservation.status == ReservationStatus.RELEASED


@pytest.mark.asyncio
async def test_external_ref_rejects_different_transfer_fingerprint(
    session: AsyncSession,
) -> None:
    tenant = await seed_ledger(session)
    await manual_recharge(session, tenant.id, 100_000, "same-business-ref")
    await session.commit()

    with pytest.raises(ConflictError, match="different transfer fingerprint"):
        await manual_recharge(session, tenant.id, 200_000, "same-business-ref")
