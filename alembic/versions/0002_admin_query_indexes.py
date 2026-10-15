"""Add composite indexes for admin query endpoints.

Revision ID: 0002
Revises: 0001
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_tenants_created_at_id", "tenants", ["created_at", "id"]),
    ("ix_api_keys_tenant_id_revoked", "api_keys", ["tenant_id", "revoked"]),
    (
        "ix_price_versions_model_id_effective_at",
        "price_versions",
        ["model_id", "effective_at"],
    ),
    (
        "ix_ledger_accounts_tenant_id_kind",
        "ledger_accounts",
        ["tenant_id", "kind"],
    ),
    (
        "ix_ledger_transfers_created_at_id",
        "ledger_transfers",
        ["created_at", "id"],
    ),
    (
        "ix_ledger_transfers_kind_created_at_id",
        "ledger_transfers",
        ["kind", "created_at", "id"],
    ),
    (
        "ix_ledger_entries_account_id_transfer_id",
        "ledger_entries",
        ["account_id", "transfer_id"],
    ),
    (
        "ix_gateway_requests_created_at_id",
        "gateway_requests",
        ["created_at", "id"],
    ),
    (
        "ix_gateway_requests_tenant_id_created_at_id",
        "gateway_requests",
        ["tenant_id", "created_at", "id"],
    ),
    (
        "ix_gateway_requests_status_created_at_id",
        "gateway_requests",
        ["status", "created_at", "id"],
    ),
    (
        "ix_gateway_requests_model_created_at_id",
        "gateway_requests",
        ["model", "created_at", "id"],
    ),
    (
        "ix_reservations_created_at_id",
        "reservations",
        ["created_at", "id"],
    ),
    (
        "ix_reservations_tenant_id_created_at_id",
        "reservations",
        ["tenant_id", "created_at", "id"],
    ),
    (
        "ix_reservations_status_created_at_id",
        "reservations",
        ["status", "created_at", "id"],
    ),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
