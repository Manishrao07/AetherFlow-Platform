"""Deep-research mode: run ``mode`` column and two new agent names.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STEP = "ck_workflow_steps_step_agent"
_CALL = "ck_llm_calls_llm_call_agent"
_OLD_STEP = "agent IN ('planner', 'retrieval', 'execution', 'review')"
_NEW_STEP = "agent IN ('planner', 'retrieval', 'execution', 'review', 'research', 'synthesizer')"
_OLD_CALL = "agent IN ('planner', 'retrieval', 'execution', 'review', 'memory')"
_NEW_CALL = (
    "agent IN ('planner', 'retrieval', 'execution', 'review', 'research', 'synthesizer', 'memory')"
)


def _swap(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("mode", sa.String(length=32), server_default="standard", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_workflow_runs_run_mode"), "workflow_runs", "mode IN ('standard', 'deep')"
    )
    _swap("workflow_steps", _STEP, _NEW_STEP)
    _swap("llm_calls", _CALL, _NEW_CALL)


def downgrade() -> None:
    op.execute("DELETE FROM workflow_runs WHERE mode = 'deep'")  # cascades to steps and calls
    op.execute("DELETE FROM llm_calls WHERE agent IN ('research', 'synthesizer')")
    _swap("llm_calls", _CALL, _OLD_CALL)
    _swap("workflow_steps", _STEP, _OLD_STEP)
    op.drop_constraint(op.f("ck_workflow_runs_run_mode"), "workflow_runs", type_="check")
    op.drop_column("workflow_runs", "mode")
