"""Commerce records. Money is integer micro-CNY; provider states are portable strings."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class CustomerUser(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "customer_users"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), unique=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        CheckConstraint("email = lower(trim(email)) AND email <> ''", name="email_normalized"),
    )

    @validates("email")
    def normalize_email(self, _key: str, value: str) -> str:
        return value.strip().lower()


class ActivationToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "activation_tokens"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("customer_users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CustomerSession(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "customer_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("customer_users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PaymentOrder(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "payment_orders"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    mode: Mapped[str] = mapped_column(String(32))
    out_trade_no: Mapped[str] = mapped_column(String(100), unique=True)
    trade_no: Mapped[str | None] = mapped_column(String(100), unique=True)
    amount_micro_cny: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    credit_transfer_id: Mapped[UUID | None] = mapped_column(ForeignKey("ledger_transfers.id"))
    checkout_url: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_payment_tenant_key"),
        CheckConstraint("mode IN ('mock', 'alipay_sandbox')", name="mode_valid"),
        CheckConstraint(
            "status IN ('CREATED', 'PENDING', 'PAID', 'CLOSED', 'UNKNOWN')", name="status_valid"
        ),
        CheckConstraint("amount_micro_cny >= 0", name="amount_nonnegative"),
        Index("ix_payment_orders_tenant_created_id", "tenant_id", "created_at", "id"),
    )


class FundLot(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "fund_lots"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    payment_order_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("payment_orders.id"), unique=True
    )
    external_ref: Mapped[str] = mapped_column(String(200), unique=True)
    credited_micro_cny: Mapped[int] = mapped_column(BigInteger, default=0)
    available_micro_cny: Mapped[int] = mapped_column(BigInteger, default=0)
    call_frozen_micro_cny: Mapped[int] = mapped_column(BigInteger, default=0)
    refund_frozen_micro_cny: Mapped[int] = mapped_column(BigInteger, default=0)
    consumed_micro_cny: Mapped[int] = mapped_column(BigInteger, default=0)
    refunded_micro_cny: Mapped[int] = mapped_column(BigInteger, default=0)
    refundable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        CheckConstraint(
            "credited_micro_cny >= 0 AND available_micro_cny >= 0 "
            "AND call_frozen_micro_cny >= 0 AND refund_frozen_micro_cny >= 0 "
            "AND consumed_micro_cny >= 0 AND refunded_micro_cny >= 0",
            name="amounts_nonnegative",
        ),
        CheckConstraint(
            "credited_micro_cny = available_micro_cny + call_frozen_micro_cny "
            "+ refund_frozen_micro_cny + consumed_micro_cny + refunded_micro_cny",
            name="source_conserved",
        ),
        Index("ix_fund_lots_tenant_fifo", "tenant_id", "created_at", "id"),
    )


class FundAllocation(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "fund_allocations"
    reservation_id: Mapped[UUID] = mapped_column(ForeignKey("reservations.id"), index=True)
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("fund_lots.id"), index=True)
    amount_micro_cny: Mapped[int] = mapped_column(BigInteger)
    # NULL = outstanding hold; 0 = released; positive = settled consumption.
    settled_micro_cny: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (
        UniqueConstraint("reservation_id", "lot_id", name="uq_allocation_reservation_lot"),
        CheckConstraint("amount_micro_cny >= 0", name="amount_nonnegative"),
        CheckConstraint(
            "settled_micro_cny IS NULL OR "
            "(settled_micro_cny >= 0 AND settled_micro_cny <= amount_micro_cny)",
            name="settled_within_allocation",
        ),
    )


class RefundRequest(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "refund_requests"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    payment_order_id: Mapped[UUID] = mapped_column(ForeignKey("payment_orders.id"), index=True)
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("fund_lots.id"), index=True)
    amount_micro_cny: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="PENDING_APPROVAL")
    out_request_no: Mapped[str] = mapped_column(String(100), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload_hash: Mapped[str] = mapped_column(String(64))
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_refund_tenant_key"),
        CheckConstraint("amount_micro_cny >= 0", name="amount_nonnegative"),
        CheckConstraint(
            "status IN ('PENDING_APPROVAL', 'PROCESSING', 'SUCCEEDED', 'REJECTED', "
            "'FAILED', 'UNKNOWN')",
            name="status_valid",
        ),
        Index("ix_refund_requests_status_created_id", "status", "created_at", "id"),
    )


class ProviderConfig(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "provider_configs"
    alias: Mapped[str] = mapped_column(String(100), unique=True)
    api_base: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    api_key_ciphertext: Mapped[str] = mapped_column(Text)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (CheckConstraint("key_version > 0", name="key_version_positive"),)


class PaymentOperation(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "payment_operations"
    refund_id: Mapped[UUID | None] = mapped_column(ForeignKey("refund_requests.id"), unique=True)
    order_id: Mapped[UUID | None] = mapped_column(ForeignKey("payment_orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        CheckConstraint("kind IN ('REFUND', 'QUERY')", name="kind_valid"),
        CheckConstraint("state IN ('PENDING', 'INFLIGHT', 'DONE', 'UNKNOWN')", name="state_valid"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("refund_id IS NOT NULL OR order_id IS NOT NULL", name="target_required"),
        Index("ix_payment_operations_due", "state", "next_attempt_at", "id"),
    )
