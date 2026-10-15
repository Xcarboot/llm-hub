import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class AccountKind(enum.StrEnum):
    AVAILABLE = "AVAILABLE"
    FROZEN = "FROZEN"
    REVENUE = "REVENUE"
    FUNDING = "FUNDING"
    REFUND_FROZEN = "REFUND_FROZEN"


class TransferKind(enum.StrEnum):
    RECHARGE = "RECHARGE"
    RESERVE = "RESERVE"
    SETTLE = "SETTLE"
    RELEASE = "RELEASE"
    REVERSAL = "REVERSAL"
    REFUND_RESERVE = "REFUND_RESERVE"
    REFUND_RELEASE = "REFUND_RELEASE"
    REFUND = "REFUND"


class ReservationStatus(enum.StrEnum):
    NEW = "NEW"
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    SETTLED = "SETTLED"
    CONSERVATIVE_SETTLED = "CONSERVATIVE_SETTLED"
    RELEASED = "RELEASED"
    REVERSED = "REVERSED"


class IdempotencyStatus(enum.StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Tenant(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (Index("ix_tenants_created_at_id", "created_at", "id"),)


class ApiKey(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "api_keys"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    prefix: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tenant: Mapped[Tenant] = relationship()
    __table_args__ = (
        Index("ix_api_keys_tenant_id_revoked", "tenant_id", "revoked"),
    )


class ModelCatalog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "model_catalog"
    public_name: Mapped[str] = mapped_column(String(100), nullable=False)
    litellm_model: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    route_order: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        UniqueConstraint("public_name", "route_order", name="uq_model_public_route"),
        CheckConstraint("route_order IN (0, 1)", name="route_order_mvp"),
    )


class PriceVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "price_versions"
    model_id: Mapped[UUID] = mapped_column(ForeignKey("model_catalog.id"), index=True)
    input_micro_cny_per_million: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_micro_cny_per_million: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[ModelCatalog] = relationship()
    __table_args__ = (
        UniqueConstraint("model_id", "effective_at", name="uq_price_model_effective"),
        CheckConstraint("input_micro_cny_per_million >= 0", name="price_input_nonnegative"),
        CheckConstraint("output_micro_cny_per_million >= 0", name="price_output_nonnegative"),
        Index("ix_price_versions_model_id_effective_at", "model_id", "effective_at"),
    )


class LedgerAccount(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "ledger_accounts"
    tenant_id: Mapped[UUID | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[AccountKind] = mapped_column(Enum(AccountKind), nullable=False)
    scope: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    __table_args__ = (
        Index("ix_ledger_accounts_tenant_id_kind", "tenant_id", "kind"),
    )


class LedgerTransfer(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "ledger_transfers"
    kind: Mapped[TransferKind] = mapped_column(Enum(TransferKind), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    posted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reversal_of_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ledger_transfers.id"), unique=True
    )
    entries: Mapped[list["LedgerEntry"]] = relationship(back_populates="transfer")
    __table_args__ = (
        Index("ix_ledger_transfers_created_at_id", "created_at", "id"),
        Index("ix_ledger_transfers_kind_created_at_id", "kind", "created_at", "id"),
    )


class LedgerEntry(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "ledger_entries"
    transfer_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_transfers.id"), index=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_accounts.id"), index=True)
    amount_micro_cny: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transfer: Mapped[LedgerTransfer] = relationship(back_populates="entries")
    account: Mapped[LedgerAccount] = relationship()
    __table_args__ = (
        CheckConstraint("amount_micro_cny <> 0", name="entry_nonzero"),
        UniqueConstraint("transfer_id", "account_id", name="uq_entry_transfer_account"),
        Index("ix_ledger_entries_account_id_transfer_id", "account_id", "transfer_id"),
    )


class Reservation(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "reservations"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    gateway_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("gateway_requests.id"), unique=True, index=True
    )
    amount_micro_cny: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus), nullable=False, default=ReservationStatus.NEW
    )
    pricing_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    selected_price_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("price_versions.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("amount_micro_cny > 0", name="reservation_positive"),
        Index("ix_reservations_created_at_id", "created_at", "id"),
        Index("ix_reservations_tenant_id_created_at_id", "tenant_id", "created_at", "id"),
        Index("ix_reservations_status_created_at_id", "status", "created_at", "id"),
    )


class GatewayRequest(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "gateway_requests"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    stream: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PROCESSING")
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        Index("ix_gateway_requests_created_at_id", "created_at", "id"),
        Index("ix_gateway_requests_tenant_id_created_at_id", "tenant_id", "created_at", "id"),
        Index("ix_gateway_requests_status_created_at_id", "status", "created_at", "id"),
        Index("ix_gateway_requests_model_created_at_id", "model", "created_at", "id"),
    )


class IdempotencyRecord(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "idempotency_records"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    gateway_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("gateway_requests.id"), unique=True
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IdempotencyStatus] = mapped_column(
        Enum(IdempotencyStatus), nullable=False, default=IdempotencyStatus.PROCESSING
    )
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    streaming: Mapped[bool] = mapped_column(Boolean, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_idempotency_tenant_key"),
    )


class AuditEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_events"
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Aggregated public model surface; commerce models depend only on Base, not this module.
from app.db.commerce_models import (  # noqa: E402, I001
    ActivationToken as ActivationToken,
    CustomerSession as CustomerSession,
    CustomerUser as CustomerUser,
    FundAllocation as FundAllocation,
    FundLot as FundLot,
    PaymentOperation as PaymentOperation,
    PaymentOrder as PaymentOrder,
    ProviderConfig as ProviderConfig,
    RefundRequest as RefundRequest,
)
