"""Source-of-funds accounting, in the caller's transaction (never commits).

Lock order starts with tenant, then reservations, then FIFO lots; ledger callers
lock accounts in scope order. Call ensure_legacy_lot BEFORE posting ledger money.
Source helpers do not post ledger entries. Roll back the transaction on any error.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InsufficientBalanceError
from app.db.models import (
    AccountKind,
    FundAllocation,
    FundLot,
    LedgerAccount,
    LedgerEntry,
    LedgerTransfer,
    PaymentOrder,
    Reservation,
    ReservationStatus,
    Tenant,
    TransferKind,
)

MAX_MICRO_CNY = 2**63 - 1
CENT_MICRO_CNY = 10_000
_OPEN = (ReservationStatus.RESERVED, ReservationStatus.DISPATCHED)
_SETTLED = (ReservationStatus.SETTLED, ReservationStatus.CONSERVATIVE_SETTLED)


def _amount(amount: int, *, positive: bool = True) -> None:
    if type(amount) is not int or not (int(positive) <= amount <= MAX_MICRO_CNY):
        raise ValueError("amount must be an integer within the nonnegative BigInteger range")


async def lock_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant:
    """Acquire the FIRST exclusive money row lock until transaction completion.

    PostgreSQL FOR NO KEY UPDATE serializes all money writers while allowing
    FK KEY SHARE locks from gateway-request inserts (no lock-upgrade deadlock).
    The tenant primary key is never changed by money operations.
    """
    tenant = await session.scalar(
        select(Tenant)
        .where(Tenant.id == tenant_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if tenant is None:
        raise ConflictError("tenant not found")
    return tenant


async def _reservation(session: AsyncSession, tenant_id: UUID, reservation_id: UUID) -> Reservation:
    await lock_tenant(session, tenant_id)
    row = await session.scalar(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.tenant_id != tenant_id:
        raise ConflictError("reservation does not belong to tenant")
    return row


async def _ledger_totals(session: AsyncSession, tenant_id: UUID) -> dict[AccountKind, int]:
    rows = await session.execute(
        select(LedgerAccount.kind, func.coalesce(func.sum(LedgerEntry.amount_micro_cny), 0))
        .outerjoin(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
        .where(LedgerAccount.tenant_id == tenant_id)
        .group_by(LedgerAccount.kind)
    )
    return {kind: int(amount) for kind, amount in rows}


async def assert_source_totals(session: AsyncSession, tenant_id: UUID) -> None:
    """Check source conservation and AVAILABLE/FROZEN/REFUND_FROZEN ledger parity."""
    await lock_tenant(session, tenant_id)
    lots = (await session.scalars(select(FundLot).where(FundLot.tenant_id == tenant_id))).all()
    for lot in lots:
        values = (
            lot.available_micro_cny,
            lot.call_frozen_micro_cny,
            lot.refund_frozen_micro_cny,
            lot.consumed_micro_cny,
            lot.refunded_micro_cny,
        )
        if any(value < 0 for value in values) or sum(values) != lot.credited_micro_cny:
            raise ConflictError("fund source conservation mismatch; reconciliation required")
    ledger = await _ledger_totals(session, tenant_id)
    for kind, total in (
        (AccountKind.AVAILABLE, sum(lot.available_micro_cny for lot in lots)),
        (AccountKind.FROZEN, sum(lot.call_frozen_micro_cny for lot in lots)),
        (AccountKind.REFUND_FROZEN, sum(lot.refund_frozen_micro_cny for lot in lots)),
    ):
        if total != ledger.get(kind, 0):
            raise ConflictError("fund source/ledger totals mismatch; reconciliation required")


async def ensure_legacy_lot(session: AsyncSession, tenant_id: UUID) -> FundLot | None:
    """Read-check first, then lazily represent old money as a nonrefundable lot.

    Outstanding reservations must EXACTLY explain FROZEN. Historical settlements
    get closed allocations as well, allowing safe settlement-only reversals.
    No ledger, reservation or existing source records are rewritten.
    """
    await lock_tenant(session, tenant_id)
    existing = await session.scalar(
        select(FundLot).where(FundLot.external_ref == f"legacy:{tenant_id}")
    )
    has_sources = (
        existing is not None
        or await session.scalar(select(FundLot.id).where(FundLot.tenant_id == tenant_id).limit(1))
        is not None
    )
    if has_sources:
        await assert_source_totals(session, tenant_id)
        return existing

    ledger = await _ledger_totals(session, tenant_id)
    available = ledger.get(AccountKind.AVAILABLE, 0)
    frozen = ledger.get(AccountKind.FROZEN, 0)
    reservations = (
        await session.scalars(
            select(Reservation)
            .where(Reservation.tenant_id == tenant_id, Reservation.status.in_((*_OPEN, *_SETTLED)))
            .order_by(Reservation.id)
            .with_for_update()
        )
    ).all()
    outstanding = [row for row in reservations if row.status in _OPEN]
    if (
        available < 0
        or frozen < 0
        or ledger.get(AccountKind.REFUND_FROZEN, 0) != 0
        or sum(row.amount_micro_cny for row in outstanding) != frozen
    ):
        raise ConflictError("legacy frozen/reservation sum mismatch; reconciliation required")

    closed: list[tuple[Reservation, int]] = []
    for row in reservations:
        if row.status not in _SETTLED:
            continue
        actual = await session.scalar(
            select(func.sum(LedgerEntry.amount_micro_cny))
            .join(LedgerTransfer, LedgerTransfer.id == LedgerEntry.transfer_id)
            .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
            .where(
                LedgerTransfer.external_ref == f"reservation:{row.id}:settle",
                LedgerTransfer.kind == TransferKind.SETTLE,
                LedgerTransfer.posted.is_(True),
                LedgerAccount.kind == AccountKind.REVENUE,
            )
        )
        # A zero-charge settlement omits the zero-valued revenue posting.
        transfer = await session.scalar(
            select(LedgerTransfer.id).where(
                LedgerTransfer.external_ref == f"reservation:{row.id}:settle",
                LedgerTransfer.kind == TransferKind.SETTLE,
                LedgerTransfer.posted.is_(True),
            )
        )
        reversed_id = (
            None
            if transfer is None
            else await session.scalar(
                select(LedgerTransfer.id).where(LedgerTransfer.reversal_of_id == transfer)
            )
        )
        if (
            transfer is None
            or reversed_id is not None
            or not 0 <= int(actual or 0) <= row.amount_micro_cny
        ):
            raise ConflictError("legacy settlement mismatch; reconciliation required")
        closed.append((row, int(actual or 0)))
    consumed = sum(actual for _, actual in closed)
    _amount(available + frozen + consumed, positive=False)
    # All consistency checks above are read-only; only now may we create sources.
    lot = FundLot(
        tenant_id=tenant_id,
        external_ref=f"legacy:{tenant_id}",
        refundable=False,
        credited_micro_cny=available + frozen + consumed,
        available_micro_cny=available,
        call_frozen_micro_cny=frozen,
        consumed_micro_cny=consumed,
        refund_frozen_micro_cny=0,
        refunded_micro_cny=0,
        created_at=datetime.now(UTC),
    )
    session.add(lot)
    await session.flush()
    session.add_all(
        [
            FundAllocation(
                reservation_id=row.id,
                lot_id=lot.id,
                amount_micro_cny=row.amount_micro_cny,
                settled_micro_cny=None,
            )
            for row in outstanding
        ]
        + [
            FundAllocation(
                reservation_id=row.id,
                lot_id=lot.id,
                amount_micro_cny=row.amount_micro_cny,
                settled_micro_cny=actual,
            )
            for row, actual in closed
        ]
    )
    await session.flush()
    return lot


async def credit_lot(
    session: AsyncSession,
    tenant_id: UUID,
    amount: int,
    external_ref: str,
    payment_order_id: UUID | None = None,
    refundable: bool = False,
) -> FundLot:
    """Idempotently credit a source only; caller posts its balanced ledger transfer."""
    await lock_tenant(session, tenant_id)
    _amount(amount)
    if not external_ref or len(external_ref) > 200 or external_ref.startswith("legacy:"):
        raise ValueError("invalid or reserved source external_ref")
    existing = await session.scalar(select(FundLot).where(FundLot.external_ref == external_ref))
    if existing is not None:
        if (
            existing.tenant_id,
            existing.credited_micro_cny,
            existing.payment_order_id,
            existing.refundable,
        ) != (tenant_id, amount, payment_order_id, refundable):
            raise ConflictError("source external_ref already has a different credit fingerprint")
        return existing
    if refundable and payment_order_id is None:
        raise ConflictError("refundable sources require a payment order")
    if payment_order_id is not None:
        order = await session.scalar(
            select(PaymentOrder).where(PaymentOrder.id == payment_order_id).with_for_update()
        )
        if order is None or order.tenant_id != tenant_id or order.amount_micro_cny != amount:
            raise ConflictError("payment order credit mismatch")
        if (
            await session.scalar(
                select(FundLot.id).where(FundLot.payment_order_id == payment_order_id)
            )
            is not None
        ):
            raise ConflictError("payment order already credited with another source reference")
    lot = FundLot(
        tenant_id=tenant_id,
        external_ref=external_ref,
        payment_order_id=payment_order_id,
        credited_micro_cny=amount,
        available_micro_cny=amount,
        refundable=refundable,
        call_frozen_micro_cny=0,
        refund_frozen_micro_cny=0,
        consumed_micro_cny=0,
        refunded_micro_cny=0,
        created_at=datetime.now(UTC),
    )
    session.add(lot)
    await session.flush()
    return lot


async def allocate(
    session: AsyncSession,
    tenant_id: UUID,
    reservation_id: UUID,
    amount: int,
) -> list[FundAllocation]:
    """FIFO available -> call-frozen, without mixing or exceeding source balances."""
    row = await _reservation(session, tenant_id, reservation_id)
    _amount(amount)
    if row.amount_micro_cny != amount:
        raise ConflictError("allocation differs from reservation ceiling")
    existing = (
        await session.scalars(
            select(FundAllocation).where(FundAllocation.reservation_id == reservation_id)
        )
    ).all()
    if existing:
        if sum(item.amount_micro_cny for item in existing) != amount:
            raise ConflictError("reservation allocation sum mismatch")
        return list(existing)
    if row.status not in (ReservationStatus.NEW, *_OPEN):
        raise ConflictError("cannot allocate a terminal reservation")
    lots = (
        await session.scalars(
            select(FundLot)
            .where(
                FundLot.tenant_id == tenant_id,
                FundLot.available_micro_cny > 0,
            )
            .order_by(FundLot.created_at, FundLot.id)
            .with_for_update()
        )
    ).all()
    if sum(lot.available_micro_cny for lot in lots) < amount:
        raise InsufficientBalanceError("insufficient available source balance")
    remaining = amount
    result = []
    for lot in lots:
        if not remaining:
            break
        part = min(lot.available_micro_cny, remaining)
        lot.available_micro_cny -= part
        lot.call_frozen_micro_cny += part
        allocation = FundAllocation(
            reservation_id=reservation_id,
            lot_id=lot.id,
            amount_micro_cny=part,
            settled_micro_cny=None,
        )
        session.add(allocation)
        result.append(allocation)
        remaining -= part
    await session.flush()
    return result


async def _allocations(
    session: AsyncSession,
    tenant_id: UUID,
    reservation_id: UUID,
) -> list[tuple[FundAllocation, FundLot]]:
    rows = (
        await session.scalars(
            select(FundAllocation).where(FundAllocation.reservation_id == reservation_id)
        )
    ).all()
    if not rows:
        raise ConflictError("reservation allocations missing; legacy reconciliation required")
    lots = (
        await session.scalars(
            select(FundLot)
            .where(
                FundLot.id.in_([row.lot_id for row in rows]),
                FundLot.tenant_id == tenant_id,
            )
            .order_by(FundLot.created_at, FundLot.id)
            .with_for_update()
        )
    ).all()
    if len(lots) != len(rows):
        raise ConflictError("allocation source tenant mismatch")
    by_lot = {row.lot_id: row for row in rows}
    return [(by_lot[lot.id], lot) for lot in lots]


async def settle_allocation(
    session: AsyncSession,
    tenant_id: UUID,
    reservation_id: UUID,
    actual: int,
) -> None:
    """Consume actual FIFO and release the unused ceiling to its original lots."""
    row = await _reservation(session, tenant_id, reservation_id)
    _amount(actual, positive=False)
    if actual > row.amount_micro_cny:
        raise ValueError("settlement exceeds reservation ceiling")
    allocations = await _allocations(session, tenant_id, reservation_id)
    if sum(item.amount_micro_cny for item, _ in allocations) != row.amount_micro_cny:
        raise ConflictError("reservation allocation sum mismatch")
    if all(item.settled_micro_cny is not None for item, _ in allocations):
        if sum(item.settled_micro_cny or 0 for item, _ in allocations) != actual:
            raise ConflictError("allocation already finalized with a different amount")
        return
    if any(
        item.settled_micro_cny is not None or lot.call_frozen_micro_cny < item.amount_micro_cny
        for item, lot in allocations
    ):
        raise ConflictError("allocation frozen balance mismatch")
    remaining = actual
    for item, lot in allocations:
        consumed = min(item.amount_micro_cny, remaining)
        lot.call_frozen_micro_cny -= item.amount_micro_cny
        lot.consumed_micro_cny += consumed
        lot.available_micro_cny += item.amount_micro_cny - consumed
        item.settled_micro_cny = consumed
        remaining -= consumed
    await session.flush()


async def release_allocation(
    session: AsyncSession,
    tenant_id: UUID,
    reservation_id: UUID,
) -> None:
    """Release an outstanding allocation, idempotently (never undo consumption)."""
    await settle_allocation(session, tenant_id, reservation_id, 0)


async def reverse_settlement_allocation(
    session: AsyncSession,
    tenant_id: UUID,
    reservation_id: UUID,
) -> int:
    """Restore consumed money to original AVAILABLE sources; ledger guards replay."""
    row = await _reservation(session, tenant_id, reservation_id)
    if row.status not in _SETTLED:
        raise ConflictError("only a settled reservation can be reversed")
    allocations = await _allocations(session, tenant_id, reservation_id)
    if any(
        item.settled_micro_cny is None or lot.consumed_micro_cny < (item.settled_micro_cny or 0)
        for item, lot in allocations
    ):
        raise ConflictError("settlement source consumption mismatch")
    actual = sum(item.settled_micro_cny or 0 for item, _ in allocations)
    for item, lot in allocations:
        consumed = item.settled_micro_cny or 0
        lot.consumed_micro_cny -= consumed
        lot.available_micro_cny += consumed
        item.settled_micro_cny = 0
    await session.flush()
    return actual


async def hold_refund(
    session: AsyncSession,
    tenant: Tenant | UUID,
    order: PaymentOrder | UUID,
    amount: int,
) -> FundLot:
    """Hold whole cents ONLY from this paid order's refundable, unused source.

    Caller serializes/idempotently records RefundRequest and posts REFUND_RESERVE
    in the same transaction; repeated calls alone are not a refund identity.
    """
    tenant_id = tenant.id if isinstance(tenant, Tenant) else tenant
    order_id = order.id if isinstance(order, PaymentOrder) else order
    await lock_tenant(session, tenant_id)
    _amount(amount)
    if amount % CENT_MICRO_CNY:
        raise ValueError("refund amount must be whole cents")
    payment = await session.scalar(
        select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update()
    )
    if payment is None or payment.tenant_id != tenant_id or payment.status != "PAID":
        raise ConflictError("refund requires this tenant's paid order")
    lot = await session.scalar(
        select(FundLot)
        .where(
            FundLot.payment_order_id == order_id,
            FundLot.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if lot is None or not lot.refundable:
        raise ConflictError("payment source is not refundable")
    if amount > lot.available_micro_cny:
        raise InsufficientBalanceError("refund exceeds this order's available unused money")
    lot.available_micro_cny -= amount
    lot.refund_frozen_micro_cny += amount
    await session.flush()
    return lot


async def finalize_refund(
    session: AsyncSession,
    tenant_id: UUID,
    lot_id: UUID,
    amount: int,
    *,
    success: bool,
) -> FundLot:
    """Refund-frozen -> refunded on success, or -> available on definitive failure.

    Refund service must guard terminal status and post ledger in the same
    transaction. UNKNOWN provider outcomes MUST remain held, not be released.
    """
    await lock_tenant(session, tenant_id)
    _amount(amount)
    if amount % CENT_MICRO_CNY:
        raise ValueError("refund amount must be whole cents")
    lot = await session.scalar(
        select(FundLot)
        .where(
            FundLot.id == lot_id,
            FundLot.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if lot is None or not lot.refundable or lot.refund_frozen_micro_cny < amount:
        raise ConflictError("refund hold/source mismatch")
    lot.refund_frozen_micro_cny -= amount
    if success:
        lot.refunded_micro_cny += amount
    else:
        lot.available_micro_cny += amount
    await session.flush()
    return lot
