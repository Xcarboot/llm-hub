from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, InsufficientBalanceError
from app.db.models import (
    AccountKind,
    LedgerAccount,
    LedgerEntry,
    LedgerTransfer,
    Reservation,
    ReservationStatus,
    TransferKind,
)
from app.services import funds


async def create_tenant_accounts(session: AsyncSession, tenant_id: UUID) -> None:
    session.add_all(
        [
            LedgerAccount(
                tenant_id=tenant_id,
                kind=AccountKind.AVAILABLE,
                scope=f"tenant:{tenant_id}:available",
            ),
            LedgerAccount(
                tenant_id=tenant_id,
                kind=AccountKind.FROZEN,
                scope=f"tenant:{tenant_id}:frozen",
            ),
            LedgerAccount(
                tenant_id=tenant_id,
                kind=AccountKind.REFUND_FROZEN,
                scope=f"tenant:{tenant_id}:refund_frozen",
            ),
        ]
    )
    await session.flush()


async def ensure_refund_account(session: AsyncSession, tenant_id: UUID) -> LedgerAccount:
    """Lazily add the new refund account for old tenants, without moving old money."""
    await funds.lock_tenant(session, tenant_id)
    scope = f"tenant:{tenant_id}:refund_frozen"
    account = await session.scalar(select(LedgerAccount).where(LedgerAccount.scope == scope))
    if account is None:
        account = LedgerAccount(tenant_id=tenant_id, kind=AccountKind.REFUND_FROZEN, scope=scope)
        session.add(account)
        await session.flush()
    return account


async def ensure_platform_accounts(session: AsyncSession) -> None:
    for kind in (AccountKind.REVENUE, AccountKind.FUNDING):
        scope = f"platform:{kind.value.lower()}"
        if await session.scalar(select(LedgerAccount).where(LedgerAccount.scope == scope)) is None:
            session.add(LedgerAccount(tenant_id=None, kind=kind, scope=scope))
    await session.flush()


async def _account(session: AsyncSession, scope: str) -> LedgerAccount:
    account = await session.scalar(
        select(LedgerAccount).where(LedgerAccount.scope == scope).with_for_update()
    )
    if account is None:
        raise RuntimeError(f"ledger account missing: {scope}")
    return account


async def _lock_accounts(session: AsyncSession, scopes: list[str]) -> dict[str, LedgerAccount]:
    rows = (
        await session.scalars(
            select(LedgerAccount)
            .where(LedgerAccount.scope.in_(sorted(scopes)))
            .order_by(LedgerAccount.scope)
            .with_for_update()
        )
    ).all()
    result = {row.scope: row for row in rows}
    missing = set(scopes) - result.keys()
    if missing:
        raise RuntimeError(f"ledger accounts missing: {sorted(missing)}")
    return result


async def account_balance(session: AsyncSession, account_id: UUID) -> int:
    amount = await session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount_micro_cny), 0)).where(
            LedgerEntry.account_id == account_id
        )
    )
    return int(amount or 0)


async def _post_transfer(
    session: AsyncSession,
    kind: TransferKind,
    external_ref: str,
    postings: list[tuple[LedgerAccount, int]],
    reversal_of_id: UUID | None = None,
) -> LedgerTransfer:
    if not postings or sum(amount for _, amount in postings) != 0:
        raise ValueError("ledger transfer must have non-empty entries summing to zero")
    if len({account.id for account, _ in postings}) != len(postings):
        raise ValueError("one posting per account is required")
    posting_fingerprint = sorted(
        (str(account.id), amount) for account, amount in postings if amount != 0
    )
    existing = await session.scalar(
        select(LedgerTransfer)
        .options(selectinload(LedgerTransfer.entries))
        .where(LedgerTransfer.external_ref == external_ref)
    )
    if existing is not None:
        existing_fingerprint = sorted(
            (str(entry.account_id), entry.amount_micro_cny) for entry in existing.entries
        )
        if (
            existing.kind != kind
            or existing.reversal_of_id != reversal_of_id
            or existing_fingerprint != posting_fingerprint
        ):
            raise ConflictError("external_ref already exists with a different transfer fingerprint")
        return existing
    transfer = LedgerTransfer(
        kind=kind,
        external_ref=external_ref,
        reversal_of_id=reversal_of_id,
        posted=True,
    )
    session.add(transfer)
    await session.flush()
    session.add_all(
        [
            LedgerEntry(
                transfer_id=transfer.id,
                account_id=account.id,
                amount_micro_cny=amount,
            )
            for account, amount in postings
            if amount != 0
        ]
    )
    await session.flush()
    actual_sum = await session.scalar(
        select(func.sum(LedgerEntry.amount_micro_cny)).where(LedgerEntry.transfer_id == transfer.id)
    )
    if int(actual_sum or 0) != 0:
        raise RuntimeError("unbalanced transfer rejected")
    return transfer


async def manual_recharge(
    session: AsyncSession, tenant_id: UUID, amount: int, external_ref: str
) -> LedgerTransfer:
    await funds.lock_tenant(session, tenant_id)
    await funds.ensure_legacy_lot(session, tenant_id)
    funds._amount(amount)
    reference = f"recharge:{external_ref}"
    if not external_ref or len(reference) > 200:
        raise ValueError("invalid recharge external_ref")
    existing = await session.scalar(
        select(LedgerTransfer.id).where(LedgerTransfer.external_ref == reference)
    )
    scopes = [f"tenant:{tenant_id}:available", "platform:funding"]
    accounts = await _lock_accounts(session, scopes)
    transfer = await _post_transfer(
        session,
        TransferKind.RECHARGE,
        reference,
        [(accounts[scopes[0]], amount), (accounts[scopes[1]], -amount)],
    )
    # Historical recharges are already represented by the legacy lot.
    if existing is None:
        await funds.credit_lot(session, tenant_id, amount, reference)
    await funds.assert_source_totals(session, tenant_id)
    return transfer


async def reserve(
    session: AsyncSession,
    tenant_id: UUID,
    gateway_request_id: UUID,
    amount: int,
    pricing_snapshot: dict[str, object],
    ttl_seconds: int,
) -> Reservation:
    await funds.lock_tenant(session, tenant_id)
    await funds.ensure_legacy_lot(session, tenant_id)
    funds._amount(amount)
    existing = await session.scalar(
        select(Reservation)
        .where(Reservation.gateway_request_id == gateway_request_id)
        .with_for_update()
    )
    if existing is not None:
        if (
            existing.tenant_id != tenant_id
            or existing.amount_micro_cny != amount
            or existing.pricing_snapshot != pricing_snapshot
        ):
            raise ConflictError("request already reserved with a different fingerprint")
        return existing
    reservation = Reservation(
        tenant_id=tenant_id,
        gateway_request_id=gateway_request_id,
        amount_micro_cny=amount,
        status=ReservationStatus.NEW,
        pricing_snapshot=pricing_snapshot,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
    )
    session.add(reservation)
    await session.flush()
    await funds.allocate(session, tenant_id, reservation.id, amount)
    available_scope = f"tenant:{tenant_id}:available"
    frozen_scope = f"tenant:{tenant_id}:frozen"
    accounts = await _lock_accounts(session, [available_scope, frozen_scope])
    if await account_balance(session, accounts[available_scope].id) < amount:
        raise InsufficientBalanceError("insufficient available balance")
    await _post_transfer(
        session,
        TransferKind.RESERVE,
        f"reservation:{reservation.id}:reserve",
        [(accounts[available_scope], -amount), (accounts[frozen_scope], amount)],
    )
    reservation.status = ReservationStatus.RESERVED
    await session.flush()
    await funds.assert_source_totals(session, tenant_id)
    return reservation


async def _locked_reservation(session: AsyncSession, reservation_id: UUID) -> Reservation:
    # This first lookup is deliberately NOT a row lock. Every money/status path
    # must acquire tenant before locking its reservation, including historical ones.
    tenant_id = await session.scalar(
        select(Reservation.tenant_id).where(Reservation.id == reservation_id)
    )
    if tenant_id is None:
        raise RuntimeError("reservation not found")
    await funds.lock_tenant(session, tenant_id)
    await funds.ensure_legacy_lot(session, tenant_id)
    return await funds._reservation(session, tenant_id, reservation_id)


async def mark_dispatched(session: AsyncSession, reservation_id: UUID) -> Reservation:
    reservation = await _locked_reservation(session, reservation_id)
    if reservation.status == ReservationStatus.RESERVED:
        reservation.status = ReservationStatus.DISPATCHED
        await session.flush()
    elif reservation.status != ReservationStatus.DISPATCHED:
        raise ConflictError(f"cannot dispatch reservation in {reservation.status.value}")
    return reservation


async def settle(
    session: AsyncSession,
    reservation_id: UUID,
    actual_amount: int,
    price_version_id: UUID,
    conservative: bool = False,
) -> Reservation:
    reservation = await _locked_reservation(session, reservation_id)
    final_status = (
        ReservationStatus.CONSERVATIVE_SETTLED if conservative else ReservationStatus.SETTLED
    )
    if reservation.status in (
        ReservationStatus.SETTLED,
        ReservationStatus.CONSERVATIVE_SETTLED,
    ):
        await funds.settle_allocation(session, reservation.tenant_id, reservation.id, actual_amount)
        return reservation
    if reservation.status not in (ReservationStatus.RESERVED, ReservationStatus.DISPATCHED):
        raise ConflictError(f"cannot settle reservation in {reservation.status.value}")
    funds._amount(actual_amount, positive=False)
    if actual_amount > reservation.amount_micro_cny:
        raise ValueError("settlement must be within the reserved ceiling")
    tenant_id = reservation.tenant_id
    await funds.settle_allocation(session, tenant_id, reservation.id, actual_amount)
    scopes = [
        f"tenant:{tenant_id}:available",
        f"tenant:{tenant_id}:frozen",
        "platform:revenue",
    ]
    accounts = await _lock_accounts(session, scopes)
    postings = [
        (accounts[scopes[1]], -reservation.amount_micro_cny),
        (accounts[scopes[2]], actual_amount),
    ]
    refund = reservation.amount_micro_cny - actual_amount
    if refund:
        postings.append((accounts[scopes[0]], refund))
    await _post_transfer(
        session,
        TransferKind.SETTLE,
        f"reservation:{reservation.id}:settle",
        postings,
    )
    reservation.status = final_status
    reservation.selected_price_version_id = price_version_id
    await session.flush()
    await funds.assert_source_totals(session, tenant_id)
    return reservation


async def release(
    session: AsyncSession,
    reservation_id: UUID,
    external_ref: str,
    *,
    timeout_release: bool = False,
) -> Reservation:
    reservation = await _locked_reservation(session, reservation_id)
    if reservation.status == ReservationStatus.RELEASED:
        return reservation
    allowed = (
        (ReservationStatus.RESERVED,)
        if timeout_release
        else (
            ReservationStatus.RESERVED,
            ReservationStatus.DISPATCHED,
        )
    )
    if reservation.status not in allowed:
        raise ConflictError(f"cannot release reservation in {reservation.status.value}")
    expires_at = reservation.expires_at
    if expires_at.tzinfo is None:  # SQLite fixtures do not preserve timezone information.
        expires_at = expires_at.replace(tzinfo=UTC)
    if timeout_release and expires_at > datetime.now(UTC):
        raise ConflictError("reservation has not expired")
    tenant_id = reservation.tenant_id
    await funds.release_allocation(session, tenant_id, reservation.id)
    scopes = [f"tenant:{tenant_id}:available", f"tenant:{tenant_id}:frozen"]
    accounts = await _lock_accounts(session, scopes)
    await _post_transfer(
        session,
        TransferKind.RELEASE,
        f"reservation:{reservation.id}:release:{external_ref}",
        [
            (accounts[scopes[1]], -reservation.amount_micro_cny),
            (accounts[scopes[0]], reservation.amount_micro_cny),
        ],
    )
    reservation.status = ReservationStatus.RELEASED
    await session.flush()
    await funds.assert_source_totals(session, tenant_id)
    return reservation


async def reverse_transfer(
    session: AsyncSession, transfer_id: UUID, external_ref: str
) -> LedgerTransfer:
    # Inspect without locking; the FIRST row lock below is always the tenant.
    original = await session.scalar(select(LedgerTransfer).where(LedgerTransfer.id == transfer_id))
    if original is None or not original.posted:
        raise RuntimeError("posted transfer not found")
    if original.kind != TransferKind.SETTLE:
        raise ConflictError("only SETTLE transfers may be reversed; use dedicated refund flows")
    parts = original.external_ref.split(":")
    if len(parts) != 3 or parts[0] != "reservation" or parts[2] != "settle":
        raise ConflictError("settlement has no attributable reservation")
    try:
        reservation_id = UUID(parts[1])
    except ValueError as exc:
        raise ConflictError("settlement has no attributable reservation") from exc
    reference = f"reversal:{external_ref}"
    if not external_ref or len(reference) > 200:
        raise ValueError("invalid reversal external_ref")
    reservation = await _locked_reservation(session, reservation_id)
    original = await session.scalar(
        select(LedgerTransfer)
        .options(selectinload(LedgerTransfer.entries).selectinload(LedgerEntry.account))
        .where(LedgerTransfer.id == transfer_id)
        .with_for_update()
    )
    assert original is not None
    existing = await session.scalar(
        select(LedgerTransfer).where(LedgerTransfer.reversal_of_id == transfer_id)
    )
    if existing is not None:
        if existing.external_ref != reference:
            raise ConflictError("settlement already reversed")
        return existing
    tenant_id = reservation.tenant_id
    available_scope = f"tenant:{tenant_id}:available"
    frozen_scope = f"tenant:{tenant_id}:frozen"
    by_scope = {entry.account.scope: entry.amount_micro_cny for entry in original.entries}
    actual = by_scope.get("platform:revenue", 0)
    expected = {frozen_scope: -reservation.amount_micro_cny}
    if actual:
        expected["platform:revenue"] = actual
    if reservation.amount_micro_cny > actual:
        expected[available_scope] = reservation.amount_micro_cny - actual
    if by_scope != expected or not 0 < actual <= reservation.amount_micro_cny:
        raise ConflictError("settlement postings are invalid or zero-charge")
    restored = await funds.reverse_settlement_allocation(session, tenant_id, reservation_id)
    if restored != actual:
        raise ConflictError("settlement ledger/source consumption mismatch")
    accounts = await _lock_accounts(session, [available_scope, "platform:revenue"])
    # Append a correction of actual consumption only. NEVER refreeze the ceiling
    # or remove the difference already returned by the original settlement.
    reversal = await _post_transfer(
        session,
        TransferKind.REVERSAL,
        reference,
        [(accounts["platform:revenue"], -actual), (accounts[available_scope], actual)],
        reversal_of_id=original.id,
    )
    reservation.status = ReservationStatus.REVERSED
    await session.flush()
    await funds.assert_source_totals(session, tenant_id)
    return reversal
