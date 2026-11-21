"""SQLAlchemy Core mirror of migration 0001 (B1).

This module describes the same six STRICT tables, four indexes and two audit
triggers as ``alembic/versions/0001_initial.py``. The migration stays
self-contained; this mirror exists so later autogenerate revisions have a
structural reference.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

AUDIT_TRIGGERS: tuple[str, ...] = (
    "CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    "CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
)

users = sa.Table(
    "users",
    metadata,
    sa.Column("user_id", sa.Text(), primary_key=True, nullable=False),
    sa.Column("requester_label", sa.Text(), nullable=False, unique=True),
    sa.Column("display_name", sa.Text(), nullable=False),
    sa.Column("created_at", sa.Text(), nullable=False),
    sqlite_strict=True,
)

categories = sa.Table(
    "categories",
    metadata,
    sa.Column("category_id", sa.Text(), primary_key=True, nullable=False),
    sa.Column("name", sa.Text(), nullable=False, unique=True),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("active", sa.Integer(), nullable=False, server_default=sa.text("1")),
    sa.CheckConstraint("active IN (0, 1)", name="ck_categories_active"),
    sqlite_strict=True,
)

confirmation_tokens = sa.Table(
    "confirmation_tokens",
    metadata,
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

tickets = sa.Table(
    "tickets",
    metadata,
    sa.Column("ticket_id", sa.Text(), primary_key=True, nullable=False),
    sa.Column(
        "requester_label",
        sa.Text(),
        sa.ForeignKey("users.requester_label"),
        nullable=False,
    ),
    sa.Column("title", sa.Text(), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("category_id", sa.Text(), sa.ForeignKey("categories.category_id"), nullable=False),
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
sa.Index("ux_tickets_token", tickets.c.confirmation_token_id, unique=True)
sa.Index("ux_tickets_key", tickets.c.requester_label, tickets.c.idempotency_key, unique=True)

idempotency_keys = sa.Table(
    "idempotency_keys",
    metadata,
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
sa.Index("ix_keys_status_age", idempotency_keys.c.status, idempotency_keys.c.claimed_at)

audit_events = sa.Table(
    "audit_events",
    metadata,
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
sa.Index("ix_audit_session_seq", audit_events.c.session_id, audit_events.c.seq)


def install_audit_triggers(connection: sa.Connection) -> None:
    """Create the append-only audit triggers on an already-created table."""
    for statement in AUDIT_TRIGGERS:
        connection.exec_driver_sql(statement)
