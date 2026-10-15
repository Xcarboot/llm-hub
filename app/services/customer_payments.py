"""Local payment simulation and source-attributed refunds.

All writes belong to the caller's transaction; callers MUST roll back on error.
No network calls, checkout URLs, provider verification, or real payments exist.
Money locks: tenant -> order -> refund -> lot -> sorted ledger accounts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, GatewayError, InfrastructureUnavailableError
from app.db.models import (
    AuditEvent,
    CustomerUser,
    FundLot,
    LedgerAccount,
    LedgerEntry,
    LedgerTransfer,
    PaymentOperation,
    PaymentOrder,
    RefundRequest,
    Tenant,
    TransferKind,
)
from app.services import funds, ledger

if TYPE_CHECKING:
    from app.core.config import Settings

MAX_PAYMENT_MICRO_CNY = 1_000_000_000  # 1000 CNY per request, NOT a real payment limit.
_HELD_STATUSES = ("PENDING_APPROVAL", "PROCESSING", "UNKNOWN")


class PaymentNotFoundError(GatewayError):
    status_code = 404
    code = "payment_not_found"


class PaymentInputError(GatewayError):
    status_code = 422
    code = "invalid_payment_input"


def payment_config(config: Settings) -> dict[str, Any]:
    enabled = config.mock_payment_enabled
    if enabled:
        notice = "本地模拟充值/退款，仅用于开发测试，不是真实支付，单笔最高1000元。"
    elif config.payment_mode == "alipay_sandbox":
        notice = "支付宝沙箱尚未接通，支付及退款执行已关闭；未进行任何支付通知验签。"
    elif config.payment_mode == "mock":
        notice = "当前环境禁止模拟支付；仅development/test可启用本地mock。"
    else:
        notice = "支付功能已关闭。"
    return {"mode": config.payment_mode, "enabled": enabled, "mock": enabled, "notice": notice}


def _require_mock(config: Settings, order: PaymentOrder | None = None) -> None:
    if not config.mock_payment_enabled or (order is not None and order.mode != "mock"):
        notice = payment_config(config)["notice"]
        if order is not None and order.mode == "alipay_sandbox":
            notice = "支付宝沙箱尚未接通，退款/支付执行已关闭；余额保持冻结，不模拟成功。"
        raise InfrastructureUnavailableError(str(notice))


def _amount(value: str | int) -> int:
    # Validate here too: services may be called without a Pydantic DTO.
    if type(value) is int:
        amount = int(value)
    elif isinstance(value, str) and 1 <= len(value) <= 19 and value.isascii() and value.isdecimal():
        amount = int(value)
    else:
        raise PaymentInputError("amount_micro_cny must be an integer decimal string")
    if not 0 < amount <= MAX_PAYMENT_MICRO_CNY or amount % funds.CENT_MICRO_CNY:
        raise PaymentInputError("amount must be positive whole cents, at most 1000000000 micro-CNY")
    return amount


def _text(value: str, maximum: int, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PaymentInputError(f"invalid {name}")


def _fingerprint(payload: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _audit(
    session: AsyncSession, actor: str, action: str, row: PaymentOrder | RefundRequest, mode: str
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            target=str(row.id),
            detail={
                "mode": mode,
                "tenant_id": str(row.tenant_id),
                "amount_micro_cny": str(row.amount_micro_cny),
                "status": row.status,
            },
        )
    )


async def _order(
    session: AsyncSession, order_id: UUID, tenant_id: UUID, *, lock: bool = False
) -> PaymentOrder:
    statement = select(PaymentOrder).where(
        PaymentOrder.id == order_id, PaymentOrder.tenant_id == tenant_id
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = await session.scalar(statement)
    if row is None:
        raise PaymentNotFoundError("payment order not found")
    return row


async def _locked_order(session: AsyncSession, order_id: UUID, tenant_id: UUID) -> PaymentOrder:
    # Ownership is checked without a lock; do not expose another tenant's rows.
    await _order(session, order_id, tenant_id)
    await funds.lock_tenant(session, tenant_id)
    return await _order(session, order_id, tenant_id, lock=True)


async def _lot(session: AsyncSession, order: PaymentOrder) -> FundLot | None:
    return await session.scalar(
        select(FundLot)
        .where(FundLot.payment_order_id == order.id, FundLot.tenant_id == order.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _assert_posting(
    session: AsyncSession, reference: str, kind: TransferKind, expected: dict[str, int]
) -> LedgerTransfer:
    transfer = await session.scalar(
        select(LedgerTransfer).where(LedgerTransfer.external_ref == reference)
    )
    if transfer is None or not transfer.posted or transfer.kind != kind:
        raise ConflictError("payment/refund ledger transfer missing or invalid")
    rows = (
        await session.execute(
            select(LedgerAccount.scope, LedgerEntry.amount_micro_cny)
            .join(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
            .where(LedgerEntry.transfer_id == transfer.id)
        )
    ).all()
    if dict(rows) != expected or transfer.reversal_of_id is not None:
        raise ConflictError("payment/refund ledger fingerprint mismatch")
    return transfer


async def _assert_paid(session: AsyncSession, order: PaymentOrder, lot: FundLot | None) -> FundLot:
    reference = f"payment:{order.id}"
    if (
        order.status != "PAID"
        or not order.trade_no
        or order.paid_at is None
        or (order.mode == "mock" and order.trade_no != f"mock_trade_{order.id.hex}")
        or lot is None
        or not lot.refundable
        or lot.payment_order_id != order.id
        or lot.tenant_id != order.tenant_id
        or lot.external_ref != reference
        or lot.credited_micro_cny != order.amount_micro_cny
    ):
        raise ConflictError("PAID payment amount/trade/source invariant mismatch")
    transfer = await _assert_posting(
        session,
        reference,
        TransferKind.RECHARGE,
        {
            f"tenant:{order.tenant_id}:available": order.amount_micro_cny,
            "platform:funding": -order.amount_micro_cny,
        },
    )
    if order.credit_transfer_id != transfer.id:
        raise ConflictError("PAID credit transfer invariant mismatch")
    await funds.assert_source_totals(session, order.tenant_id)
    return lot


async def create_payment(
    session: AsyncSession,
    tenant_id: UUID,
    amount_micro_cny: str | int,
    idempotency_key: str,
    config: Settings,
    *,
    actor: str,
) -> PaymentOrder:
    amount = _amount(amount_micro_cny)
    _text(idempotency_key, 200, "idempotency_key")
    _require_mock(config)
    await funds.lock_tenant(session, tenant_id)
    fingerprint = _fingerprint({"amount_micro_cny": str(amount), "mode": "mock"})
    existing = await session.scalar(
        select(PaymentOrder)
        .where(PaymentOrder.tenant_id == tenant_id, PaymentOrder.idempotency_key == idempotency_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        if (
            existing.payload_hash != fingerprint
            or existing.amount_micro_cny != amount
            or existing.mode != "mock"
        ):
            raise ConflictError("payment idempotency key has a different payload")
        return existing
    row = PaymentOrder(
        id=uuid4(),
        tenant_id=tenant_id,
        mode="mock",
        out_trade_no=f"mock_{uuid4().hex}",
        amount_micro_cny=amount,
        idempotency_key=idempotency_key,
        payload_hash=fingerprint,
        status="PENDING",
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    _audit(session, actor, "payment.mock.create", row, row.mode)
    await session.flush()
    return row


async def confirm_mock_payment(
    session: AsyncSession, tenant_id: UUID, order_id: UUID, config: Settings, *, actor: str
) -> PaymentOrder:
    order = await _locked_order(session, order_id, tenant_id)
    _require_mock(config, order)
    _amount(order.amount_micro_cny)
    await funds.ensure_legacy_lot(session, tenant_id)  # BEFORE either money representation.
    await ledger.ensure_refund_account(session, tenant_id)
    lot = await _lot(session, order)
    if order.status == "PAID":
        await _assert_paid(session, order, lot)
        return order
    if order.status != "PENDING":
        raise ConflictError(f"cannot confirm payment in {order.status}")
    reference = f"payment:{order.id}"
    if (
        lot is not None
        or order.trade_no is not None
        or order.credit_transfer_id is not None
        or order.paid_at is not None
        or await session.scalar(
            select(LedgerTransfer.id).where(LedgerTransfer.external_ref == reference)
        )
        is not None
    ):
        raise ConflictError("pending payment already has credit artifacts; reconcile first")
    # Source FIRST, accounts SECOND. Do NOT call manual_recharge (it credits its own lot).
    lot = await funds.credit_lot(
        session, tenant_id, order.amount_micro_cny, reference, order.id, refundable=True
    )
    scopes = [f"tenant:{tenant_id}:available", "platform:funding"]
    accounts = await ledger._lock_accounts(session, scopes)
    transfer = await ledger._post_transfer(
        session,
        TransferKind.RECHARGE,
        reference,
        [
            (accounts[scopes[0]], order.amount_micro_cny),
            (accounts[scopes[1]], -order.amount_micro_cny),
        ],
    )
    order.trade_no = f"mock_trade_{order.id.hex}"
    order.credit_transfer_id = transfer.id
    order.paid_at = datetime.now(UTC)
    order.status = "PAID"
    await session.flush()
    await _assert_paid(session, order, lot)
    _audit(session, actor, "payment.mock.confirm", order, order.mode)
    await session.flush()
    return order


async def create_refund(
    session: AsyncSession,
    tenant_id: UUID,
    order_id: UUID,
    amount_micro_cny: str | int,
    reason: str,
    idempotency_key: str,
    config: Settings,
    *,
    actor: str,
) -> RefundRequest:
    amount = _amount(amount_micro_cny)
    _text(reason, 2000, "reason")
    _text(idempotency_key, 200, "idempotency_key")
    order = await _locked_order(session, order_id, tenant_id)
    if order.mode == "mock":
        _require_mock(config, order)
    elif config.payment_mode != "alipay_sandbox":
        raise InfrastructureUnavailableError("payment/refund feature is disabled")
    fingerprint = _fingerprint(
        {"payment_order_id": str(order_id), "amount_micro_cny": str(amount), "reason": reason}
    )
    existing = await session.scalar(
        select(RefundRequest)
        .where(
            RefundRequest.tenant_id == tenant_id, RefundRequest.idempotency_key == idempotency_key
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        if (
            existing.payload_hash != fingerprint
            or existing.payment_order_id != order_id
            or existing.amount_micro_cny != amount
            or existing.reason != reason
        ):
            raise ConflictError("refund idempotency key has a different payload")
        return existing
    # A refund request only reserves funds. Sandbox approvals remain unavailable.
    await funds.ensure_legacy_lot(session, tenant_id)
    await ledger.ensure_refund_account(session, tenant_id)
    await _assert_paid(session, order, await _lot(session, order))
    lot = await funds.hold_refund(session, tenant_id, order_id, amount)
    row = RefundRequest(
        id=uuid4(),
        tenant_id=tenant_id,
        payment_order_id=order_id,
        lot_id=lot.id,
        amount_micro_cny=amount,
        reason=reason,
        status="PENDING_APPROVAL",
        out_request_no=f"mock_refund_{uuid4().hex}" if order.mode == "mock" else uuid4().hex,
        idempotency_key=idempotency_key,
        payload_hash=fingerprint,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    scopes = [f"tenant:{tenant_id}:available", f"tenant:{tenant_id}:refund_frozen"]
    accounts = await ledger._lock_accounts(session, scopes)
    await ledger._post_transfer(
        session,
        TransferKind.REFUND_RESERVE,
        f"refund:{row.id}:reserve",
        [(accounts[scopes[0]], -amount), (accounts[scopes[1]], amount)],
    )
    await funds.assert_source_totals(session, tenant_id)
    _audit(session, actor, "refund.request", row, order.mode)
    await session.flush()
    return row


async def _locked_refund(
    session: AsyncSession, refund_id: UUID
) -> tuple[RefundRequest, PaymentOrder]:
    target = (
        await session.execute(
            select(RefundRequest.tenant_id, RefundRequest.payment_order_id).where(
                RefundRequest.id == refund_id
            )
        )
    ).one_or_none()
    if target is None:
        raise PaymentNotFoundError("refund not found")
    tenant_id, order_id = target
    await funds.lock_tenant(session, tenant_id)
    order = await _order(session, order_id, tenant_id, lock=True)
    row = await session.scalar(
        select(RefundRequest)
        .where(RefundRequest.id == refund_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.tenant_id != tenant_id or row.payment_order_id != order_id:
        raise ConflictError("refund ownership invariant mismatch")
    return row, order


async def _assert_hold(session: AsyncSession, row: RefundRequest, order: PaymentOrder) -> FundLot:
    lot = await _assert_paid(session, order, await _lot(session, order))
    held = int(
        await session.scalar(
            select(func.coalesce(func.sum(RefundRequest.amount_micro_cny), 0)).where(
                RefundRequest.lot_id == lot.id, RefundRequest.status.in_(_HELD_STATUSES)
            )
        )
        or 0
    )
    if row.lot_id != lot.id or lot.refund_frozen_micro_cny != held or held < row.amount_micro_cny:
        raise ConflictError("refund hold/source invariant mismatch")
    await _assert_posting(
        session,
        f"refund:{row.id}:reserve",
        TransferKind.REFUND_RESERVE,
        {
            f"tenant:{row.tenant_id}:available": -row.amount_micro_cny,
            f"tenant:{row.tenant_id}:refund_frozen": row.amount_micro_cny,
        },
    )
    return lot


async def approve_refund(
    session: AsyncSession, refund_id: UUID, reason: str, config: Settings, *, actor: str = "admin"
) -> RefundRequest:
    _text(reason, 2000, "reason")
    row, order = await _locked_refund(session, refund_id)
    _require_mock(config, order)  # No provider requests, regardless of credentials/settings.
    if row.status == "SUCCEEDED":
        if row.decision_reason != reason:
            raise ConflictError("refund approval reason differs from the original decision")
        await _assert_paid(session, order, await _lot(session, order))
        await _assert_posting(
            session,
            f"refund:{row.id}:complete",
            TransferKind.REFUND,
            {
                f"tenant:{row.tenant_id}:refund_frozen": -row.amount_micro_cny,
                "platform:funding": row.amount_micro_cny,
            },
        )
        operation = await session.scalar(
            select(PaymentOperation).where(PaymentOperation.refund_id == row.id)
        )
        if (
            operation is None
            or operation.kind != "REFUND"
            or operation.state != "DONE"
            or operation.order_id != order.id
        ):
            raise ConflictError("successful refund operation invariant mismatch")
        return row
    if row.status != "PENDING_APPROVAL":
        raise ConflictError(f"cannot approve refund in {row.status}; funds remain held")
    _amount(row.amount_micro_cny)
    if (
        await session.scalar(
            select(PaymentOperation.id).where(PaymentOperation.refund_id == row.id)
        )
        is not None
    ):
        raise ConflictError("refund has an existing operation; reconcile without releasing funds")
    lot = await _assert_hold(session, row, order)
    row.status = "PROCESSING"
    row.decision_reason = reason
    row.approved_at = datetime.now(UTC)
    operation = PaymentOperation(
        refund_id=row.id, order_id=order.id, kind="REFUND", state="INFLIGHT", attempts=1
    )
    session.add(operation)
    _audit(session, actor, "refund.mock.approve", row, order.mode)
    await session.flush()
    # Definitive local success, atomically. UNKNOWN is never synthesized or released.
    await funds.finalize_refund(session, row.tenant_id, lot.id, row.amount_micro_cny, success=True)
    scopes = [f"tenant:{row.tenant_id}:refund_frozen", "platform:funding"]
    accounts = await ledger._lock_accounts(session, scopes)
    await ledger._post_transfer(
        session,
        TransferKind.REFUND,
        f"refund:{row.id}:complete",
        [(accounts[scopes[0]], -row.amount_micro_cny), (accounts[scopes[1]], row.amount_micro_cny)],
    )
    row.status = "SUCCEEDED"
    row.provider_ref = f"mock_refund_{row.id.hex}"
    row.completed_at = datetime.now(UTC)
    operation.state = "DONE"
    await session.flush()
    await funds.assert_source_totals(session, row.tenant_id)
    _audit(session, actor, "refund.mock.succeed", row, order.mode)
    await session.flush()
    return row


async def reject_refund(
    session: AsyncSession, refund_id: UUID, reason: str, *, actor: str = "admin"
) -> RefundRequest:
    _text(reason, 2000, "reason")
    row, order = await _locked_refund(session, refund_id)
    if row.status != "PENDING_APPROVAL":
        raise ConflictError(f"cannot reject refund in {row.status}; funds must not be released")
    _amount(row.amount_micro_cny)
    if (
        await session.scalar(
            select(PaymentOperation.id).where(PaymentOperation.refund_id == row.id)
        )
        is not None
    ):
        raise ConflictError("refund has an existing operation; funds must not be released")
    lot = await _assert_hold(session, row, order)
    await funds.finalize_refund(session, row.tenant_id, lot.id, row.amount_micro_cny, success=False)
    scopes = [f"tenant:{row.tenant_id}:refund_frozen", f"tenant:{row.tenant_id}:available"]
    accounts = await ledger._lock_accounts(session, scopes)
    await ledger._post_transfer(
        session,
        TransferKind.REFUND_RELEASE,
        f"refund:{row.id}:release",
        [(accounts[scopes[0]], -row.amount_micro_cny), (accounts[scopes[1]], row.amount_micro_cny)],
    )
    row.status = "REJECTED"
    row.decision_reason = reason
    row.completed_at = datetime.now(UTC)
    await session.flush()
    await funds.assert_source_totals(session, row.tenant_id)
    _audit(session, actor, "refund.reject", row, order.mode)
    await session.flush()
    return row


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    ).isoformat()


def _payment_view(order: PaymentOrder, lot: FundLot | None) -> dict[str, Any]:
    available = lot.available_micro_cny if lot is not None else 0
    refundable = (
        available // funds.CENT_MICRO_CNY * funds.CENT_MICRO_CNY
        if (lot is not None and lot.refundable and order.status == "PAID")
        else 0
    )
    return {
        "id": str(order.id),
        "mode": order.mode,
        "status": order.status,
        "out_trade_no": order.out_trade_no,
        "trade_no": order.trade_no,
        "amount_micro_cny": str(order.amount_micro_cny),
        "created_at": _timestamp(order.created_at),
        "paid_at": _timestamp(order.paid_at),
        "fund_available_micro_cny": str(available),
        "refundable_micro_cny": str(refundable),
        "refundable_cents": str(refundable // funds.CENT_MICRO_CNY),
        "refunded_micro_cny": str(lot.refunded_micro_cny if lot is not None else 0),
        "refund_frozen_micro_cny": str(lot.refund_frozen_micro_cny if lot is not None else 0),
    }


def _refund_view(row: RefundRequest, order: PaymentOrder, lot: FundLot | None) -> dict[str, Any]:
    payment = _payment_view(order, lot)
    return {
        "id": str(row.id),
        "payment_order_id": str(row.payment_order_id),
        "mode": order.mode,
        "status": row.status,
        "out_trade_no": order.out_trade_no,
        "out_request_no": row.out_request_no,
        "amount_micro_cny": str(row.amount_micro_cny),
        "reason": row.reason,
        "decision_reason": row.decision_reason,
        "created_at": _timestamp(row.created_at),
        "approved_at": _timestamp(row.approved_at),
        "completed_at": _timestamp(row.completed_at),
        **{
            key: payment[key]
            for key in (
                "fund_available_micro_cny",
                "refundable_micro_cny",
                "refundable_cents",
                "refunded_micro_cny",
                "refund_frozen_micro_cny",
            )
        },
    }


async def get_payment(session: AsyncSession, tenant_id: UUID, order_id: UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            select(PaymentOrder, FundLot)
            .outerjoin(
                FundLot,
                and_(
                    FundLot.payment_order_id == PaymentOrder.id,
                    FundLot.tenant_id == PaymentOrder.tenant_id,
                ),
            )
            .where(PaymentOrder.id == order_id, PaymentOrder.tenant_id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise PaymentNotFoundError("payment order not found")
    return _payment_view(row[0], row[1])


async def get_refund(session: AsyncSession, tenant_id: UUID, refund_id: UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            select(RefundRequest, PaymentOrder, FundLot)
            .join(PaymentOrder, PaymentOrder.id == RefundRequest.payment_order_id)
            .outerjoin(
                FundLot,
                and_(
                    FundLot.id == RefundRequest.lot_id, FundLot.tenant_id == RefundRequest.tenant_id
                ),
            )
            .where(
                RefundRequest.id == refund_id,
                RefundRequest.tenant_id == tenant_id,
                PaymentOrder.tenant_id == tenant_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise PaymentNotFoundError("refund not found")
    return _refund_view(row[0], row[1], row[2])


def _cursor_encode(created_at: datetime, row_id: UUID) -> str:
    return (
        base64.urlsafe_b64encode(
            json.dumps({"created_at": _timestamp(created_at), "id": str(row_id)}).encode()
        )
        .decode()
        .rstrip("=")
    )


def _cursor_decode(value: str) -> tuple[datetime, UUID]:
    try:
        if not 1 <= len(value) <= 1024:
            raise ValueError
        payload = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(payload, dict) or set(payload) != {"created_at", "id"}:
            raise ValueError
        if not isinstance(payload["created_at"], str) or not isinstance(payload["id"], str):
            raise ValueError
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        return created_at.astimezone(UTC), UUID(payload["id"])
    except (ValueError, TypeError, KeyError, binascii.Error) as exc:
        raise PaymentInputError("invalid cursor") from exc


async def list_records(
    session: AsyncSession,
    *,
    refunds: bool = False,
    tenant_id: UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    admin: bool = False,
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise PaymentInputError("limit must be between 1 and 100")
    if not admin and tenant_id is None:
        raise PaymentNotFoundError("tenant not found")
    model = RefundRequest if refunds else PaymentOrder
    statement: Any  # The two explicit projections have different tuple shapes.
    if refunds:
        statement = select(RefundRequest, PaymentOrder, FundLot, Tenant.name, CustomerUser.email)
        statement = statement.join(
            PaymentOrder,
            and_(
                PaymentOrder.id == RefundRequest.payment_order_id,
                PaymentOrder.tenant_id == RefundRequest.tenant_id,
            ),
        )
        statement = statement.outerjoin(
            FundLot,
            and_(FundLot.id == RefundRequest.lot_id, FundLot.tenant_id == RefundRequest.tenant_id),
        )
    else:
        statement = select(PaymentOrder, FundLot, Tenant.name, CustomerUser.email).outerjoin(
            FundLot,
            and_(
                FundLot.payment_order_id == PaymentOrder.id,
                FundLot.tenant_id == PaymentOrder.tenant_id,
            ),
        )
    statement = statement.join(Tenant, Tenant.id == model.tenant_id).outerjoin(
        CustomerUser, CustomerUser.tenant_id == model.tenant_id
    )
    if tenant_id is not None:
        statement = statement.where(model.tenant_id == tenant_id)
    if status is not None:
        _text(status, 32, "status")
        statement = statement.where(model.status == status)
    if cursor is not None:
        created_at, row_id = _cursor_decode(cursor)
        statement = statement.where(
            or_(
                model.created_at < created_at,
                and_(model.created_at == created_at, model.id < row_id),
            )
        )
    rows = (
        await session.execute(
            statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
        )
    ).all()
    visible = rows[:limit]
    items = []
    for row in visible:
        item = _refund_view(row[0], row[1], row[2]) if refunds else _payment_view(row[0], row[1])
        if admin:
            item["tenant"] = {"id": str(row[0].tenant_id), "name": row[-2], "email": row[-1]}
            item["tenant_id"] = str(row[0].tenant_id)
        items.append(item)
    has_more = len(rows) > limit
    return {
        "items": items,
        "has_more": has_more,
        "next_cursor": _cursor_encode(visible[-1][0].created_at, visible[-1][0].id)
        if has_more and visible
        else None,
    }
