"""Add tickets and notification_outbox (side-effect tables for tools, idempotent by key).

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("severity", sa.String(length=8), server_default="SEV3", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="OPEN", nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('SEV1','SEV2','SEV3','SEV4')", name=op.f("ck_tickets_severity_valid")
        ),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name=op.f("ck_tickets_status_valid")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_tickets_run_id_workflow_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_tickets_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("idempotency_key", name="uq_tickets_idempotency_key"),
    )
    op.create_index("ix_tickets_run_id", "tickets", ["run_id"], unique=False)
    op.create_index("ix_tickets_user_created", "tickets", ["user_id", "created_at"], unique=False)
    op.create_table(
        "notification_outbox",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("channel", sa.String(length=16), server_default="email", nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_notification_outbox_run_id_workflow_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_outbox_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_outbox")),
        sa.UniqueConstraint("idempotency_key", name="uq_notification_outbox_idempotency_key"),
    )
    op.create_index(
        "ix_notification_outbox_run_id", "notification_outbox", ["run_id"], unique=False
    )
    op.create_index(
        "ix_notification_outbox_user_created",
        "notification_outbox",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_user_created", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_run_id", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_index("ix_tickets_user_created", table_name="tickets")
    op.drop_index("ix_tickets_run_id", table_name="tickets")
    op.drop_table("tickets")
