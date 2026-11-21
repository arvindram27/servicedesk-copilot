"""B1 initial schema: six STRICT tables, four indexes, two audit triggers.

Self-contained by design: this revision imports no application code, so a later
refactor of app.db.schema cannot rewrite history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

AUDIT_TRIGGERS: tuple[str, ...] = (
    "CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    "CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("requester_label", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sqlite_strict=True,
    )
    op.create_table(
        "categories",
        sa.Column("category_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("active IN (0, 1)", name="ck_categories_active"),
        sqlite_strict=True,
    )
    op.create_table(
        "confirmation_tokens",
        sa.Column("token_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "requester_label",
            sa.Text(),
            sa.ForeignKey("users.requester_label"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("draft_hash", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.Text(), nullable=True),
        sa.Column("ticket_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(consumed_at IS NULL) = (ticket_id IS NULL)",
            name="ck_tokens_consumed_pairing",
        ),
        sqlite_strict=True,
    )
    op.create_table(
        "tickets",
        sa.Column("ticket_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "requester_label",
            sa.Text(),
            sa.ForeignKey("users.requester_label"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "category_id", sa.Text(), sa.ForeignKey("categories.category_id"), nullable=False
        ),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "confirmation_token_id",
            sa.Text(),
            sa.ForeignKey("confirmation_tokens.token_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_tickets_priority",
        ),
        sqlite_strict=True,
    )
    op.create_index("ux_tickets_token", "tickets", ["confirmation_token_id"], unique=True)
    op.create_index(
        "ux_tickets_key", "tickets", ["requester_label", "idempotency_key"], unique=True
    )
    op.create_table(
        "idempotency_keys",
        sa.Column(
            "requester_label",
            sa.Text(),
            sa.ForeignKey("users.requester_label"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload_fingerprint", sa.Text(), nullable=False),
        sa.Column("confirmation_token_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("claimed_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("owner_run_id", sa.Text(), nullable=False),
        sa.Column("owner_generation", sa.Integer(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("ticket_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("requester_label", "idempotency_key", name="pk_idempotency_keys"),
        sa.CheckConstraint("status IN ('in_flight', 'completed')", name="ck_keys_status"),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL"
            " AND response_status IS NOT NULL AND response_json IS NOT NULL)"
            " OR "
            "(status = 'in_flight' AND completed_at IS NULL"
            " AND response_status IS NULL AND response_json IS NULL)",
            name="ck_keys_status_fields",
        ),
        sqlite_strict=True,
    )
    op.create_index("ix_keys_status_age", "idempotency_keys", ["status", "claimed_at"])
    op.create_table(
        "audit_events",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Text(), nullable=False, unique=True),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("requester_label", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("node", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sqlite_strict=True,
        sqlite_autoincrement=True,
    )
    op.create_index("ix_audit_session_seq", "audit_events", ["session_id", "seq"])
    for statement in AUDIT_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_audit_session_seq", table_name="audit_events")
    op.drop_table("audit_events")  # drops its triggers with the table
    op.drop_index("ix_keys_status_age", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_index("ux_tickets_key", table_name="tickets")
    op.drop_index("ux_tickets_token", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("confirmation_tokens")
    op.drop_table("categories")
    op.drop_table("users")
