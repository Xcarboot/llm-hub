"""Seal ledger postings to the transaction that created their transfer.

Revision ID: 0004
Revises: 0003

Adding the volatile default backfills existing transfers with the migration's
transaction ID without issuing financial UPDATEs. After migration commit they
are sealed too. txid_current() includes the epoch, unlike the wrapping xid type.
Transfer insertion must precede entry insertion in the same outer transaction;
multiple flushes and savepoints do not seal a transfer prematurely. The original
immutable and deferred balance triggers remain in force.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return

    op.add_column(
        "ledger_transfers",
        sa.Column(
            "posting_transaction_id",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("txid_current()"),
        ),
    )
    op.execute("""
    CREATE FUNCTION assert_ledger_posting_transaction() RETURNS trigger AS $$
    DECLARE parent_transaction_id bigint;
    BEGIN
      SELECT posting_transaction_id INTO parent_transaction_id
      FROM ledger_transfers WHERE id = NEW.transfer_id;
      IF parent_transaction_id IS DISTINCT FROM txid_current() THEN
        RAISE EXCEPTION 'ledger transfer % is sealed to its posting transaction', NEW.transfer_id;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER ledger_entries_posting_transaction
      BEFORE INSERT ON ledger_entries
      FOR EACH ROW EXECUTE FUNCTION assert_ledger_posting_transaction()
    """)


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return

    # Only sealing metadata is removed; financial rows and old protections stay.
    op.execute("DROP TRIGGER ledger_entries_posting_transaction ON ledger_entries")
    op.execute("DROP FUNCTION assert_ledger_posting_transaction()")
    op.drop_column("ledger_transfers", "posting_transaction_id")
