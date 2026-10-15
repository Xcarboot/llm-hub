"""Add commerce and conserved funding sources without rewriting old money.

Revision ID: 0003
Revises: 0002

Preflight only reads old balances/reservations. Sources and refund accounts are
created lazily by services. Immutable ledger/price triggers remain untouched.
PostgreSQL enum extensions are individually committed and safe to retry.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def uuid_pk() -> sa.Column:
    return sa.Column("id", sa.Uuid(), primary_key=True)


def created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def fk(name: str, target: str, *, nullable: bool = False, unique: bool = False) -> sa.Column:
    return sa.Column(name, sa.Uuid(), sa.ForeignKey(target), nullable=nullable, unique=unique)


def money(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.BigInteger(), nullable=nullable)


def text(name: str, length: int, *, nullable: bool = False, unique: bool = False) -> sa.Column:
    return sa.Column(name, sa.String(length), nullable=nullable, unique=unique)


def timestamp(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        # No INSERT/UPDATE/backfill: stop rather than invent an origin for corrupt
        # historical frozen money (including old unsafe generic reversals).
        op.execute("""
        DO $$ BEGIN
          IF EXISTS (
            WITH balances AS (
              SELECT a.tenant_id,
                COALESCE(SUM(e.amount_micro_cny) FILTER (WHERE a.kind = 'AVAILABLE'), 0) AS available,
                COALESCE(SUM(e.amount_micro_cny) FILTER (WHERE a.kind = 'FROZEN'), 0) AS frozen
              FROM ledger_accounts a LEFT JOIN ledger_entries e ON e.account_id = a.id
              WHERE a.tenant_id IS NOT NULL GROUP BY a.tenant_id
            ), holds AS (
              SELECT tenant_id, SUM(amount_micro_cny) AS frozen
              FROM reservations WHERE status IN ('RESERVED', 'DISPATCHED') GROUP BY tenant_id
            )
            SELECT 1 FROM tenants t LEFT JOIN balances b ON b.tenant_id = t.id
              LEFT JOIN holds h ON h.tenant_id = t.id
            WHERE COALESCE(b.available, 0) < 0 OR COALESCE(b.frozen, 0) < 0
              OR COALESCE(b.frozen, 0) <> COALESCE(h.frozen, 0)
          ) THEN
            RAISE EXCEPTION 'legacy frozen/reservation sum mismatch; reconcile before migration 0003';
          END IF;
        END; $$;
        """)
        for enum_name, value in (
            ("accountkind", "REFUND_FROZEN"),
            ("transferkind", "REFUND_RESERVE"),
            ("transferkind", "REFUND_RELEASE"),
            ("transferkind", "REFUND"),
        ):
            with op.get_context().autocommit_block():
                op.execute(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")

    op.create_table(
        "customer_users",
        uuid_pk(),
        created_at(),
        fk("tenant_id", "tenants.id", unique=True),
        text("email", 320, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.CheckConstraint("email = lower(trim(email)) AND email <> ''", name="email_normalized"),
    )
    op.create_table(
        "activation_tokens",
        uuid_pk(),
        created_at(),
        fk("user_id", "customer_users.id"),
        text("token_hash", 64, unique=True),
        timestamp("expires_at", nullable=False),
        timestamp("used_at"),
    )
    op.create_index("ix_activation_tokens_user_id", "activation_tokens", ["user_id"])
    op.create_table(
        "customer_sessions",
        uuid_pk(),
        created_at(),
        fk("user_id", "customer_users.id"),
        text("token_hash", 64, unique=True),
        text("csrf_hash", 64),
        timestamp("expires_at", nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_customer_sessions_user_id", "customer_sessions", ["user_id"])
    op.create_index("ix_customer_sessions_expires_at", "customer_sessions", ["expires_at"])
    op.create_table(
        "payment_orders",
        uuid_pk(),
        created_at(),
        fk("tenant_id", "tenants.id"),
        text("mode", 32),
        text("out_trade_no", 100, unique=True),
        text("trade_no", 100, unique=True, nullable=True),
        money("amount_micro_cny"),
        text("idempotency_key", 200),
        text("payload_hash", 64),
        text("status", 32),
        fk("credit_transfer_id", "ledger_transfers.id", nullable=True),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        timestamp("paid_at"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_payment_tenant_key"),
        sa.CheckConstraint("mode IN ('mock', 'alipay_sandbox')", name="mode_valid"),
        sa.CheckConstraint(
            "status IN ('CREATED', 'PENDING', 'PAID', 'CLOSED', 'UNKNOWN')", name="status_valid"
        ),
        sa.CheckConstraint("amount_micro_cny >= 0", name="amount_nonnegative"),
    )
    op.create_index("ix_payment_orders_tenant_id", "payment_orders", ["tenant_id"])
    op.create_index(
        "ix_payment_orders_tenant_created_id", "payment_orders", ["tenant_id", "created_at", "id"]
    )
    op.create_table(
        "fund_lots",
        uuid_pk(),
        created_at(),
        fk("tenant_id", "tenants.id"),
        fk("payment_order_id", "payment_orders.id", nullable=True, unique=True),
        text("external_ref", 200, unique=True),
        money("credited_micro_cny"),
        money("available_micro_cny"),
        money("call_frozen_micro_cny"),
        money("refund_frozen_micro_cny"),
        money("consumed_micro_cny"),
        money("refunded_micro_cny"),
        sa.Column("refundable", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "credited_micro_cny >= 0 AND available_micro_cny >= 0 "
            "AND call_frozen_micro_cny >= 0 AND refund_frozen_micro_cny >= 0 "
            "AND consumed_micro_cny >= 0 AND refunded_micro_cny >= 0",
            name="amounts_nonnegative",
        ),
        sa.CheckConstraint(
            "credited_micro_cny = available_micro_cny + call_frozen_micro_cny "
            "+ refund_frozen_micro_cny + consumed_micro_cny + refunded_micro_cny",
            name="source_conserved",
        ),
    )
    op.create_index("ix_fund_lots_tenant_id", "fund_lots", ["tenant_id"])
    op.create_index("ix_fund_lots_tenant_fifo", "fund_lots", ["tenant_id", "created_at", "id"])
    op.create_table(
        "fund_allocations",
        uuid_pk(),
        created_at(),
        fk("reservation_id", "reservations.id"),
        fk("lot_id", "fund_lots.id"),
        money("amount_micro_cny"),
        money("settled_micro_cny", nullable=True),
        sa.UniqueConstraint("reservation_id", "lot_id", name="uq_allocation_reservation_lot"),
        sa.CheckConstraint("amount_micro_cny >= 0", name="amount_nonnegative"),
        sa.CheckConstraint(
            "settled_micro_cny IS NULL OR (settled_micro_cny >= 0 "
            "AND settled_micro_cny <= amount_micro_cny)",
            name="settled_within_allocation",
        ),
    )
    op.create_index("ix_fund_allocations_reservation_id", "fund_allocations", ["reservation_id"])
    op.create_index("ix_fund_allocations_lot_id", "fund_allocations", ["lot_id"])
    op.create_table(
        "refund_requests",
        uuid_pk(),
        created_at(),
        fk("tenant_id", "tenants.id"),
        fk("payment_order_id", "payment_orders.id"),
        fk("lot_id", "fund_lots.id"),
        money("amount_micro_cny"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        text("status", 32),
        text("out_request_no", 100, unique=True),
        text("idempotency_key", 200),
        text("payload_hash", 64),
        text("provider_ref", 200, nullable=True),
        timestamp("approved_at"),
        timestamp("completed_at"),
        text("error_code", 100, nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_refund_tenant_key"),
        sa.CheckConstraint("amount_micro_cny >= 0", name="amount_nonnegative"),
        sa.CheckConstraint(
            "status IN ('PENDING_APPROVAL', 'PROCESSING', 'SUCCEEDED', "
            "'REJECTED', 'FAILED', 'UNKNOWN')",
            name="status_valid",
        ),
    )
    for column in ("tenant_id", "payment_order_id", "lot_id"):
        op.create_index(f"ix_refund_requests_{column}", "refund_requests", [column])
    op.create_index(
        "ix_refund_requests_status_created_id", "refund_requests", ["status", "created_at", "id"]
    )
    op.create_table(
        "provider_configs",
        uuid_pk(),
        text("alias", 100, unique=True),
        sa.Column("api_base", sa.Text(), nullable=False),
        text("model", 200),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("key_version > 0", name="key_version_positive"),
    )
    op.create_table(
        "payment_operations",
        uuid_pk(),
        created_at(),
        fk("refund_id", "refund_requests.id", nullable=True, unique=True),
        fk("order_id", "payment_orders.id", nullable=True),
        text("kind", 16),
        text("state", 16),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        text("error_code", 100, nullable=True),
        sa.CheckConstraint("kind IN ('REFUND', 'QUERY')", name="kind_valid"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'INFLIGHT', 'DONE', 'UNKNOWN')", name="state_valid"
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        sa.CheckConstraint("refund_id IS NOT NULL OR order_id IS NOT NULL", name="target_required"),
    )
    op.create_index("ix_payment_operations_order_id", "payment_operations", ["order_id"])
    op.create_index(
        "ix_payment_operations_due", "payment_operations", ["state", "next_attempt_at", "id"]
    )


def downgrade() -> None:
    raise RuntimeError(
        "Financial migration 0003 is irreversible: downgrade must not destroy financial data"
    )
