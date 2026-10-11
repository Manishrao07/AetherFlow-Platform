"""Allow 'memory' as an LLM caller (conversation summarisation is attributed to it).

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_llm_calls_llm_call_agent"


def upgrade() -> None:
    op.drop_constraint(op.f(_NAME), "llm_calls", type_="check")
    op.create_check_constraint(
        op.f(_NAME),
        "llm_calls",
        "agent IN ('planner', 'retrieval', 'execution', 'review', 'memory')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM llm_calls WHERE agent = 'memory'")
    op.drop_constraint(op.f(_NAME), "llm_calls", type_="check")
    op.create_check_constraint(
        op.f(_NAME), "llm_calls", "agent IN ('planner', 'retrieval', 'execution', 'review')"
    )
