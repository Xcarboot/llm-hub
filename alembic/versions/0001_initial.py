"""Initial immutable ledger and gateway schema.

Revision ID: 0001
Revises:
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

account_kind = sa.Enum("AVAILABLE", "FROZEN", "REVENUE", "FUNDING", name="accountkind")
transfer_kind = sa.Enum("RECHARGE", "RESERVE", "SETTLE", "RELEASE", "REVERSAL", name="transferkind")
reservation_status = sa.Enum(
    "NEW", "RESERVED", "DISPATCHED", "SETTLED", "CONSERVATIVE_SETTLED", "RELEASED", "REVERSED",
    name="reservationstatus",
)
idempotency_status = sa.Enum("PROCESSING", "COMPLETED", "FAILED", name="idempotencystatus")


def uuid_pk() -> sa.Column:
    return sa.Column("id", sa.Uuid(), primary_key=True)


def created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table("tenants", uuid_pk(), sa.Column("name", sa.String(200), nullable=False, unique=True), sa.Column("active", sa.Boolean(), nullable=False), created_at())
    op.create_table(
        "api_keys", uuid_pk(), sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False, unique=True), sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("revoked", sa.Boolean(), nullable=False), created_at(),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_table(
        "model_catalog", uuid_pk(), sa.Column("public_name", sa.String(100), nullable=False),
        sa.Column("litellm_model", sa.String(200), nullable=False, unique=True), sa.Column("route_order", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False), created_at(),
        sa.CheckConstraint("route_order IN (0, 1)", name="ck_model_catalog_route_order_mvp"),
        sa.UniqueConstraint("public_name", "route_order", name="uq_model_public_route"),
    )
    op.create_table(
        "price_versions", uuid_pk(), sa.Column("model_id", sa.Uuid(), sa.ForeignKey("model_catalog.id"), nullable=False),
        sa.Column("input_micro_cny_per_million", sa.BigInteger(), nullable=False),
        sa.Column("output_micro_cny_per_million", sa.BigInteger(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False), created_at(),
        sa.CheckConstraint("input_micro_cny_per_million >= 0", name="ck_price_versions_price_input_nonnegative"),
        sa.CheckConstraint("output_micro_cny_per_million >= 0", name="ck_price_versions_price_output_nonnegative"),
        sa.UniqueConstraint("model_id", "effective_at", name="uq_price_model_effective"),
    )
    op.create_index("ix_price_versions_model_id", "price_versions", ["model_id"])
    op.create_table(
        "ledger_accounts", uuid_pk(), sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("kind", account_kind, nullable=False), sa.Column("scope", sa.String(100), nullable=False, unique=True), created_at(),
    )
    op.create_index("ix_ledger_accounts_tenant_id", "ledger_accounts", ["tenant_id"])
    op.create_table(
        "ledger_transfers", uuid_pk(), sa.Column("kind", transfer_kind, nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=False, unique=True), sa.Column("posted", sa.Boolean(), nullable=False),
        sa.Column("reversal_of_id", sa.Uuid(), sa.ForeignKey("ledger_transfers.id"), nullable=True, unique=True), created_at(),
    )
    op.create_table(
        "ledger_entries", uuid_pk(), sa.Column("transfer_id", sa.Uuid(), sa.ForeignKey("ledger_transfers.id"), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("ledger_accounts.id"), nullable=False),
        sa.Column("amount_micro_cny", sa.BigInteger(), nullable=False), created_at(),
        sa.CheckConstraint("amount_micro_cny <> 0", name="ck_ledger_entries_entry_nonzero"),
        sa.UniqueConstraint("transfer_id", "account_id", name="uq_entry_transfer_account"),
    )
    op.create_index("ix_ledger_entries_transfer_id", "ledger_entries", ["transfer_id"])
    op.create_index("ix_ledger_entries_account_id", "ledger_entries", ["account_id"])
    op.create_table(
        "gateway_requests", uuid_pk(), sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("model", sa.String(100), nullable=False), sa.Column("stream", sa.Boolean(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False), sa.Column("status", sa.String(40), nullable=False),
        sa.Column("usage_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True), sa.Column("error_code", sa.String(100), nullable=True), created_at(),
    )
    op.create_index("ix_gateway_requests_tenant_id", "gateway_requests", ["tenant_id"])
    op.create_table(
        "reservations", uuid_pk(), sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("gateway_request_id", sa.Uuid(), sa.ForeignKey("gateway_requests.id"), nullable=False, unique=True),
        sa.Column("amount_micro_cny", sa.BigInteger(), nullable=False), sa.Column("status", reservation_status, nullable=False),
        sa.Column("pricing_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("selected_price_version_id", sa.Uuid(), sa.ForeignKey("price_versions.id"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), created_at(),
        sa.CheckConstraint("amount_micro_cny > 0", name="ck_reservations_reservation_positive"),
    )
    op.create_index("ix_reservations_tenant_id", "reservations", ["tenant_id"])
    op.create_index("ix_reservations_gateway_request_id", "reservations", ["gateway_request_id"], unique=True)
    op.create_table(
        "idempotency_records", uuid_pk(), sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("gateway_request_id", sa.Uuid(), sa.ForeignKey("gateway_requests.id"), nullable=False, unique=True),
        sa.Column("payload_hash", sa.String(64), nullable=False), sa.Column("status", idempotency_status, nullable=False),
        sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("streaming", sa.Boolean(), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True), created_at(),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_idempotency_tenant_key"),
    )
    op.create_index("ix_idempotency_records_tenant_id", "idempotency_records", ["tenant_id"])
    op.create_table(
        "audit_events", uuid_pk(), sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=False), sa.Column("target", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False), created_at(),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.execute("""
    CREATE FUNCTION reject_immutable_change() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'immutable posted ledger/price record cannot be changed';
    END; $$ LANGUAGE plpgsql;
    """)
    for table in ("ledger_entries", "ledger_transfers", "price_versions"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_immutable_change()")
    op.execute("""
    CREATE FUNCTION assert_transfer_balanced() RETURNS trigger AS $$
    DECLARE total bigint; entry_count integer;
    BEGIN
      SELECT COALESCE(SUM(amount_micro_cny), 0), COUNT(*) INTO total, entry_count
      FROM ledger_entries WHERE transfer_id = NEW.transfer_id;
      IF total <> 0 OR entry_count < 2 THEN
        RAISE EXCEPTION 'ledger transfer % is not balanced', NEW.transfer_id;
      END IF;
      RETURN NULL;
    END; $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER ledger_transfer_balanced
      AFTER INSERT ON ledger_entries DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION assert_transfer_balanced()
    """)
    op.execute("""
    CREATE FUNCTION assert_new_transfer_balanced() RETURNS trigger AS $$
    DECLARE total bigint; entry_count integer;
    BEGIN
      SELECT COALESCE(SUM(amount_micro_cny), 0), COUNT(*) INTO total, entry_count
      FROM ledger_entries WHERE transfer_id = NEW.id;
      IF total <> 0 OR entry_count < 2 THEN
        RAISE EXCEPTION 'ledger transfer % is not balanced', NEW.id;
      END IF;
      RETURN NULL;
    END; $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER new_ledger_transfer_balanced
      AFTER INSERT ON ledger_transfers DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION assert_new_transfer_balanced()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS new_ledger_transfer_balanced ON ledger_transfers")
    op.execute("DROP FUNCTION IF EXISTS assert_new_transfer_balanced()")
    op.execute("DROP TRIGGER IF EXISTS ledger_transfer_balanced ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS assert_transfer_balanced()")
    for table in ("ledger_entries", "ledger_transfers", "price_versions"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_immutable_change()")
    for table in ("audit_events", "idempotency_records", "reservations", "gateway_requests", "ledger_entries", "ledger_transfers", "ledger_accounts", "price_versions", "model_catalog", "api_keys", "tenants"):
        op.drop_table(table)
    idempotency_status.drop(op.get_bind(), checkfirst=True)
    reservation_status.drop(op.get_bind(), checkfirst=True)
    transfer_kind.drop(op.get_bind(), checkfirst=True)
    account_kind.drop(op.get_bind(), checkfirst=True)
