"""Orchestrator support: step revisions, run leases, run_events log.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_WHERE = "status IN ('PLANNING','EXECUTING','REVIEWING')"
_NEW_WHERE = "status IN ('CREATED','PLANNING','EXECUTING','REVIEWING')"


def _recreate_recoverable_index(where: str) -> None:
    op.drop_index("ix_workflow_runs_recoverable", table_name="workflow_runs")
    op.create_index(
        "ix_workflow_runs_recoverable",
        "workflow_runs",
        ["status", "updated_at"],
        postgresql_where=sa.text(where),
    )


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("lease_owner", sa.String(length=80), nullable=True))
    op.add_column(
        "workflow_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    # WHY: a run that was CREATED but never picked up (crash right after insert) must be
    # found by recovery too.
    _recreate_recoverable_index(_NEW_WHERE)

    op.add_column(
        "workflow_steps",
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_workflow_steps_step_revision_non_negative"), "workflow_steps", "revision >= 0"
    )
    op.drop_constraint("uq_workflow_steps_run_step_key", "workflow_steps", type_="unique")
    op.create_unique_constraint(
        "uq_workflow_steps_run_revision_step_key",
        "workflow_steps",
        ["run_id", "revision", "step_key"],
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_run_events_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_events")),
    )
    op.create_index("ix_run_events_run_id_id", "run_events", ["run_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_run_events_run_id_id", table_name="run_events")
    op.drop_table("run_events")
    # keep only the newest revision's steps so the old uniqueness rule can be restored
    op.execute(
        "DELETE FROM workflow_steps s USING workflow_steps n "
        "WHERE s.run_id = n.run_id AND s.step_key = n.step_key AND s.revision < n.revision"
    )
    op.drop_constraint("uq_workflow_steps_run_revision_step_key", "workflow_steps", type_="unique")
    op.create_unique_constraint(
        "uq_workflow_steps_run_step_key", "workflow_steps", ["run_id", "step_key"]
    )
    op.drop_constraint(op.f("ck_workflow_steps_step_revision_non_negative"), "workflow_steps")
    op.drop_column("workflow_steps", "revision")
    _recreate_recoverable_index(_OLD_WHERE)
    op.drop_column("workflow_runs", "lease_expires_at")
    op.drop_column("workflow_runs", "lease_owner")
