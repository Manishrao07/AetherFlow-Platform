"""Initial schema: 15 tables, constraints, indexes, reference roles, audit-log immutability.

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


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("actor_email", sa.String(length=320), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=16), server_default="success", nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('success','failure','denied')", name=op.f("ck_audit_logs_outcome_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_action_created", "audit_logs", ["action", "created_at"], unique=False
    )
    op.create_index(
        "ix_audit_logs_actor_created", "audit_logs", ["actor_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_audit_logs_created_at",
        "audit_logs",
        [sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"], unique=False
    )
    op.create_table(
        "roles",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("name", name=op.f("uq_roles_name")),
    )
    op.create_table(
        "users",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        "uq_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True
    )
    op.create_table(
        "conversations",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "summarized_until_message_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'ARCHIVED')", name=op.f("ck_conversations_conversation_status")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "ix_conversations_user_status_updated",
        "conversations",
        ["user_id", "status", "updated_at"],
        unique=False,
    )
    op.create_table(
        "documents",
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("source", sa.String(length=500), server_default="upload", nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="READY", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('READY', 'FAILED')", name=op.f("ck_documents_document_status")
        ),
        sa.CheckConstraint("chunk_count >= 0", name=op.f("ck_documents_chunk_count_non_negative")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_documents_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("owner_id", "content_sha256", name="uq_documents_owner_sha256"),
    )
    op.create_table(
        "memories",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), server_default="FACT", nullable=False),
        sa.Column("importance", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', key || ' ' || content)", persisted=True),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('FACT', 'PREFERENCE')", name=op.f("ck_memories_memory_kind")),
        sa.CheckConstraint("importance BETWEEN 1 AND 5", name=op.f("ck_memories_importance_range")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_memories_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
        sa.UniqueConstraint("user_id", "key", name="uq_memories_user_key"),
    )
    op.create_index(
        "ix_memories_search", "memories", ["search_vector"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column("granted_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            name=op.f("fk_user_roles_granted_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_user_roles_role_id_roles"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"], unique=False)
    op.create_table(
        "document_chunks",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "chunk_index >= 0", name=op.f("ck_document_chunks_chunk_index_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_index"),
    )
    op.create_index(
        "ix_document_chunks_search",
        "document_chunks",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "workflow_runs",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="CREATED", nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("revision_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_revisions", sa.Integer(), server_default="2", nullable=False),
        sa.Column("review_verdict", sa.String(length=32), nullable=True),
        sa.Column("review_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "review_verdict IN ('APPROVED', 'NEEDS_REVISION', 'REJECTED')",
            name=op.f("ck_workflow_runs_review_verdict"),
        ),
        sa.CheckConstraint(
            "status IN ('CREATED', 'PLANNING', 'AWAITING_APPROVAL', 'EXECUTING', 'REVIEWING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_workflow_runs_run_status"),
        ),
        sa.CheckConstraint(
            "review_score IS NULL OR (review_score >= 0 AND review_score <= 1)",
            name=op.f("ck_workflow_runs_review_score_range"),
        ),
        sa.CheckConstraint(
            "revision_count >= 0 AND revision_count <= max_revisions",
            name=op.f("ck_workflow_runs_revision_bounds"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_workflow_runs_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_workflow_runs_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_workflow_runs_user_idempotency_key"
        ),
    )
    op.create_index(
        "ix_workflow_runs_conversation_id", "workflow_runs", ["conversation_id"], unique=False
    )
    op.create_index(
        "ix_workflow_runs_recoverable",
        "workflow_runs",
        ["status", "updated_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('PLANNING','EXECUTING','REVIEWING')"),
    )
    op.create_index(
        "ix_workflow_runs_user_created",
        "workflow_runs",
        ["user_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')", name=op.f("ck_messages_message_role")
        ),
        sa.CheckConstraint("token_count >= 0", name=op.f("ck_messages_token_count_non_negative")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_messages_run_id_workflow_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_messages_run_id", "messages", ["run_id"], unique=False)
    op.create_table(
        "workflow_steps",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("tool", sa.String(length=100), nullable=True),
        sa.Column(
            "inputs", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("risk_level", sa.String(length=32), server_default="LOW", nullable=False),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "agent IN ('planner', 'retrieval', 'execution', 'review')",
            name=op.f("ck_workflow_steps_step_agent"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('LOW', 'MEDIUM', 'HIGH')",
            name=op.f("ck_workflow_steps_step_risk_level"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'AWAITING_APPROVAL', 'SUCCEEDED', 'FAILED', 'SKIPPED')",
            name=op.f("ck_workflow_steps_step_status"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts",
            name=op.f("ck_workflow_steps_attempts_bounds"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1", name=op.f("ck_workflow_steps_max_attempts_positive")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_workflow_steps_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_steps")),
        sa.UniqueConstraint("run_id", "step_key", name="uq_workflow_steps_run_step_key"),
    )
    op.create_index(
        "ix_workflow_steps_run_position", "workflow_steps", ["run_id", "position"], unique=False
    )
    op.create_table(
        "approvals",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("step_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=False),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('PLAN', 'STEP')", name=op.f("ck_approvals_approval_kind")),
        sa.CheckConstraint(
            "risk_level IN ('LOW', 'MEDIUM', 'HIGH')", name=op.f("ck_approvals_approval_risk_level")
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')",
            name=op.f("ck_approvals_approval_status"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('APPROVED','REJECTED') OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name=op.f("ck_approvals_decision_complete"),
        ),
        sa.CheckConstraint(
            "decided_by IS NULL OR decided_by <> requested_by",
            name=op.f("ck_approvals_separation_of_duties"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name=op.f("fk_approvals_decided_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_approvals_requested_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_approvals_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["workflow_steps.id"],
            name=op.f("fk_approvals_step_id_workflow_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_index(
        "ix_approvals_pending_expires",
        "approvals",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"], unique=False)
    op.create_index("ix_approvals_step_id", "approvals", ["step_id"], unique=False)
    op.create_table(
        "llm_calls",
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("step_id", sa.UUID(), nullable=True),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False
        ),
        sa.Column("cached", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "agent IN ('planner', 'retrieval', 'execution', 'review')",
            name=op.f("ck_llm_calls_llm_call_agent"),
        ),
        sa.CheckConstraint(
            "status IN ('SUCCEEDED', 'FAILED', 'TIMEOUT')",
            name=op.f("ck_llm_calls_llm_call_status"),
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND latency_ms >= 0 AND cost_usd >= 0",
            name=op.f("ck_llm_calls_usage_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_llm_calls_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["workflow_steps.id"],
            name=op.f("fk_llm_calls_step_id_workflow_steps"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"], unique=False)
    op.create_index("ix_llm_calls_run_created", "llm_calls", ["run_id", "created_at"], unique=False)
    op.create_index("ix_llm_calls_step_id", "llm_calls", ["step_id"], unique=False)
    op.create_table(
        "step_dependencies",
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column("depends_on_step_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "step_id <> depends_on_step_id", name=op.f("ck_step_dependencies_no_self_dependency")
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_step_id"],
            ["workflow_steps.id"],
            name=op.f("fk_step_dependencies_depends_on_step_id_workflow_steps"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["workflow_steps.id"],
            name=op.f("fk_step_dependencies_step_id_workflow_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("step_id", "depends_on_step_id", name=op.f("pk_step_dependencies")),
    )
    op.create_index(
        "ix_step_dependencies_depends_on", "step_dependencies", ["depends_on_step_id"], unique=False
    )
    op.create_table(
        "tool_executions",
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column(
            "input", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('SUCCEEDED', 'FAILED', 'TIMEOUT')",
            name=op.f("ck_tool_executions_tool_call_status"),
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_tool_executions_attempt_positive")),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["workflow_steps.id"],
            name=op.f("fk_tool_executions_step_id_workflow_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_executions")),
        sa.UniqueConstraint("idempotency_key", name="uq_tool_executions_idempotency_key"),
    )
    op.create_index("ix_tool_executions_step_id", "tool_executions", ["step_id"], unique=False)
    op.create_index(
        "ix_tool_executions_tool_status_created",
        "tool_executions",
        ["tool_name", "status", "created_at"],
        unique=False,
    )

    # --- Reference data: the four RBAC roles ------------------------------------------------
    # WHY in the migration: roles are part of the schema's meaning (FK targets), so every
    # environment, including a bare `alembic upgrade head`, must have them.
    roles = sa.table("roles", sa.column("name", sa.String), sa.column("description", sa.String))
    op.bulk_insert(
        roles,
        [
            {"name": "admin", "description": "Full access incl. user and role management"},
            {"name": "approver", "description": "Can approve or reject high-risk runs"},
            {"name": "operator", "description": "Can create and run workflows"},
            {"name": "viewer", "description": "Read-only access"},
        ],
    )

    # --- Append-only audit log ------------------------------------------------------------
    # WHY triggers: even a bug, a rogue admin endpoint or a hand-typed SQL statement from the
    # app's DB role cannot rewrite history. (A superuser could drop the trigger - production
    # additionally grants the app role INSERT/SELECT only. See docs/architecture.md.)
    op.execute(
        """
        CREATE FUNCTION audit_logs_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER audit_logs_no_update_delete BEFORE UPDATE OR DELETE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION audit_logs_block_mutation()"
    )
    op.execute(
        "CREATE TRIGGER audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_block_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update_delete ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_block_mutation()")
    op.drop_index("ix_tool_executions_tool_status_created", table_name="tool_executions")
    op.drop_index("ix_tool_executions_step_id", table_name="tool_executions")
    op.drop_table("tool_executions")
    op.drop_index("ix_step_dependencies_depends_on", table_name="step_dependencies")
    op.drop_table("step_dependencies")
    op.drop_index("ix_llm_calls_step_id", table_name="llm_calls")
    op.drop_index("ix_llm_calls_run_created", table_name="llm_calls")
    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_index("ix_approvals_step_id", table_name="approvals")
    op.drop_index("ix_approvals_run_id", table_name="approvals")
    op.drop_index(
        "ix_approvals_pending_expires",
        table_name="approvals",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_table("approvals")
    op.drop_index("ix_workflow_steps_run_position", table_name="workflow_steps")
    op.drop_table("workflow_steps")
    op.drop_index("ix_messages_run_id", table_name="messages")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_workflow_runs_user_created", table_name="workflow_runs")
    op.drop_index(
        "ix_workflow_runs_recoverable",
        table_name="workflow_runs",
        postgresql_where=sa.text("status IN ('PLANNING','EXECUTING','REVIEWING')"),
    )
    op.drop_index("ix_workflow_runs_conversation_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index("ix_document_chunks_search", table_name="document_chunks", postgresql_using="gin")
    op.drop_table("document_chunks")
    op.drop_index("ix_user_roles_role_id", table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_index("ix_memories_search", table_name="memories", postgresql_using="gin")
    op.drop_table("memories")
    op.drop_table("documents")
    op.drop_index("ix_conversations_user_status_updated", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("roles")
    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action_created", table_name="audit_logs")
    op.drop_table("audit_logs")
