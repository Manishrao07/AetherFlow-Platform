"""Schemas for workflow runs, steps, the timeline and approvals."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    AgentName,
    ApprovalKind,
    ApprovalStatus,
    CallStatus,
    LLMCaller,
    ReviewVerdict,
    RiskLevel,
    RunMode,
    RunStatus,
    StepStatus,
)


class WorkflowCreate(BaseModel):
    goal: str = Field(min_length=3, max_length=4000)
    conversation_id: uuid.UUID | None = None
    max_revisions: int | None = Field(default=None, ge=0, le=5)
    # "deep" = split the goal into sub-questions, research each, write a cited report.
    mode: RunMode = RunMode.STANDARD
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "goal": "Check the SEV2 incident policy, open a ticket and email ops@example.com",
            }
        }
    )


class WorkflowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID | None
    goal: str
    mode: RunMode
    status: RunStatus
    plan: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None
    revision_count: int
    max_revisions: int
    review_verdict: ReviewVerdict | None
    review_score: Decimal | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    step_key: str
    revision: int
    position: int
    agent: AgentName
    tool: str | None
    inputs: dict[str, Any]
    output: dict[str, Any] | None
    status: StepStatus
    risk_level: RiskLevel
    requires_approval: bool
    attempts: int
    max_attempts: int
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class ToolExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    step_id: uuid.UUID
    tool_name: str
    attempt: int
    input: dict[str, Any]
    output: dict[str, Any] | None
    status: CallStatus
    error: str | None
    duration_ms: int | None
    created_at: datetime


class LLMCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    step_id: uuid.UUID | None
    agent: LLMCaller
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    cost_usd: Decimal
    cached: bool
    status: CallStatus
    error: str | None
    created_at: datetime


class ApprovalOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    kind: ApprovalKind
    status: ApprovalStatus
    risk_level: RiskLevel
    reason: str
    requested_by: uuid.UUID
    requester_email: str
    decided_by: uuid.UUID | None
    comment: str | None
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime
    goal: str | None = None
    plan: dict[str, Any] | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    type: str
    data: dict[str, Any]
    created_at: datetime


class TimelineOut(BaseModel):
    run: WorkflowOut
    steps: list[StepOut]
    tool_executions: list[ToolExecutionOut]
    llm_calls: list[LLMCallOut]
    approvals: list[ApprovalOut]
    events: list[EventOut]
    totals: dict[str, Any]


class ApproveBody(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)
    model_config = ConfigDict(
        json_schema_extra={"example": {"comment": "Reviewed the recipients."}}
    )


class RejectBody(BaseModel):
    # WHY required: a rejection without a reason is useless to the requester.
    comment: str = Field(min_length=1, max_length=1000)
    model_config = ConfigDict(json_schema_extra={"example": {"comment": "Wrong recipient list."}})


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    model_config = ConfigDict(
        json_schema_extra={"example": {"question": "Which source supports the refund rule?"}}
    )


class ReportSourceOut(BaseModel):
    n: int
    tool: str
    citation: str
    url: str | None = None


class AskResponse(BaseModel):
    answer: str
    citations: list[ReportSourceOut]


class SimilarRunOut(BaseModel):
    id: uuid.UUID
    goal: str
    mode: RunMode
    similarity: float
    finished_at: datetime | None
